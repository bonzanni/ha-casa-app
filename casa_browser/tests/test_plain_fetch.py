"""A plain public read: no browser, no cookie. What reaches a site is counted where the bytes
arrive, through the real proxy (``proxyharness``); what must not move is read from the fake
browser's log."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import proxyharness as h
from casa_browser.downloads import (
    FALLBACK_USER_AGENT,
    Downloader,
    DownloadRefused,
    PlainAnswer,
    html_text,
    pick_fields,
)
from casa_browser.handles import HandleRegistry
from casa_browser.masking import mask_whole
from casa_browser.protocol import (
    MAX_READ,
    Admission,
    ProtocolError,
    RunSpec,
    SiteGrant,
    parse_fetch,
)
from casa_browser.store import Store
from controllerfakes import Rig, spec

SITE, OTHER, LAN = "site.example", "other.example", "lan.example"
HOSTS = [SITE, OTHER, LAN, "a.example"]
AGENT = "Mozilla/5.0 synthetic browser"
PAGE = (b"<!doctype html><html><head><title>Opening hours</title><style>p{color:red}</style>"
        b"<script>var secret = 'dropped';</script></head><body><h1>Hours</h1>"
        b"<p>Open <b>daily</b>&nbsp;from nine. <a href=\"/more?x=1\">More</a></p>"
        b"<ul><li>One</li><li>Two <a href='#top'>top</a></li></ul></body></html>")
TOKEN = "Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"  # gitleaks:allow - synthetic; proves masking applies


def _policy():
    return h.account_policy(sites=(h.site(origins=(f"https://{SITE}",), login=()),))


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "state.db")
    the_spec = RunSpec("rq", "req-1", "account", "read", "read", (SiteGrant("s1", "account"),), "",
                       Admission("operator_telegram", "m1", "sha256:" + "0" * 64))
    run, _ = store.create_run(the_spec, "r1")
    return SimpleNamespace(store=store, reg=HandleRegistry(), tmp=tmp_path, run=run)


def _downloader(env, hz):
    return Downloader(env.store, env.reg, env.tmp / "artifacts", proxy_url=hz.download_url,
                      ca_file=env.tmp / "ca" / "mitmproxy-ca-cert.pem")


def _with_proxy(env, body, resolver=None):
    async def go():
        async with h.Harness(env.tmp, HOSTS, resolver=resolver) as hz:
            hz.addon.install(_policy())
            return await body(hz)
    return h.run(go)


def _leftovers(env):
    d = env.tmp / "artifacts" / "files"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# ------------------------------------------------------------------ through the real proxy


def test_a_page_is_stored_with_the_address_that_served_it(env):
    async def body(hz):
        fx = hz.fixture
        fx.pages["/old"] = ("text/plain", b"", {"Location": f"https://{OTHER}/new?x=1"})
        fx.status["/old"] = 302
        fx.pages["/new"] = ("text/html", PAGE, {})
        answer = await _downloader(env, hz).fetch_plain(env.run, f"https://{SITE}/old", _policy(),
                                                        AGENT)
        return answer, [r[1:3] for r in fx.receipts.requests], list(fx.receipts.cookies)

    answer, requests, cookies = _with_proxy(env, body)
    assert answer.reason is None and answer.url == f"https://{OTHER}/new?x=1"
    assert answer.record.mime == "text/html" and answer.record.source_handle is None
    assert Path(answer.record.private_path).read_bytes() == PAGE
    assert requests == [(SITE, "/old"), (OTHER, "/new?x=1")]
    assert cookies == []


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503, 204, 206])
def test_a_refusal_names_the_sites_status_on_one_request_and_keeps_nothing(env, status):
    async def body(hz):
        fx = hz.fixture
        fx.pages["/p"] = ("text/plain", b"no", {})
        fx.status["/p"] = status
        answer = await _downloader(env, hz).fetch_plain(env.run, f"https://{SITE}/p", _policy(),
                                                        AGENT)
        return answer, [r[1:3] for r in fx.receipts.requests]

    answer, requests = _with_proxy(env, body)
    assert answer.reason == f"http_{status}" and answer.record is None
    assert requests == [(SITE, "/p")]        # the status came from the one request sent
    assert _leftovers(env) == []


def test_an_answer_compressed_against_the_request_keeps_nothing(env):
    async def body(hz):
        hz.fixture.pages["/p"] = ("text/html", PAGE, {"Content-Encoding": "gzip"})
        return await _downloader(env, hz).fetch_plain(env.run, f"https://{SITE}/p", _policy(),
                                                      AGENT)

    assert _with_proxy(env, body).reason == "http_200"
    assert _leftovers(env) == []


@pytest.mark.parametrize("address, reason", [
    (f"https://{h.ip4(127, 0, 0, 1)}/admin", "blocked"), (f"https://{h.PRIVATE_IP}/admin", "blocked"),
    ("https://[::1]/admin", "blocked"), (f"https://{LAN}/admin", "http_403")])
def test_a_private_address_is_never_sent(env, address, reason):
    """A private literal is refused by the policy before a send; a name that resolves to a
    private address is answered 403 by the proxy's floor. Counted at the relay."""
    resolver = h.fixed_resolver({SITE: [h.FIXTURE_IP], OTHER: [h.FIXTURE_IP],
                                 "a.example": [h.FIXTURE_IP], LAN: [h.PRIVATE_IP]})

    async def body(hz):
        answer = await _downloader(env, hz).fetch_plain(env.run, address, _policy(), AGENT)
        return answer, hz.fixture.receipts.requests, hz.fixture.receipts.connections

    answer, requests, connections = _with_proxy(env, body, resolver)
    assert answer.record is None and answer.reason == reason
    assert list(requests) == [] and connections == 0


@pytest.mark.parametrize("target, reason", [
    (f"https://{h.PRIVATE_IP}/admin", "blocked"), (f"https://{LAN}/admin", "http_403"),
    ("http://localhost/admin", "blocked")])
def test_a_redirect_to_a_private_address_is_never_followed(env, target, reason):
    resolver = h.fixed_resolver({SITE: [h.FIXTURE_IP], OTHER: [h.FIXTURE_IP],
                                 "a.example": [h.FIXTURE_IP], LAN: [h.PRIVATE_IP]})

    async def body(hz):
        fx = hz.fixture
        fx.pages["/go"] = ("text/plain", b"", {"Location": target})
        fx.status["/go"] = 302
        answer = await _downloader(env, hz).fetch_plain(env.run, f"https://{SITE}/go", _policy(),
                                                        AGENT)
        return answer, [r[1:3] for r in fx.receipts.requests]

    answer, requests = _with_proxy(env, body, resolver)
    assert answer.record is None and answer.url is None and answer.reason == reason
    assert requests == [(SITE, "/go")]       # the redirect's own request, nothing after it


def test_a_type_outside_the_document_set_is_not_kept(env):
    """A public link download of such a type waits for the check; a plain fetch keeps nothing
    rather than becoming the way around it. Nothing is left on disk."""
    async def body(hz):
        hz.fixture.pages["/tool.exe"] = ("application/octet-stream", b"MZ\x90\x00" * 50, {})
        return await _downloader(env, hz).fetch_plain(env.run, f"https://{SITE}/tool.exe",
                                                      _policy(), AGENT)

    assert _with_proxy(env, body).reason == "not_document"
    assert _leftovers(env) == [] and env.store.get_file("x") is None


def test_a_body_past_the_bound_is_refused_and_leaves_nothing(env):
    async def body(hz):
        hz.fixture.pages["/p"] = ("text/plain", b"x" * 5000, {})
        dl = _downloader(env, hz)
        dl.max_file = 1000
        return await dl.fetch_plain(env.run, f"https://{SITE}/p", _policy(), AGENT)

    with pytest.raises(DownloadRefused, match="file_too_large"):
        _with_proxy(env, body)
    assert _leftovers(env) == []


def test_reading_the_signed_in_sites_own_origin_carries_no_cookie(tmp_path):
    """The whole path, during an account session: Chrome holds the site's cookie, the address is
    the account site's own, and nothing of it reaches the site; Chrome is not even asked."""
    async def go():
        async with h.Harness(tmp_path, HOSTS) as hz:
            rig = await Rig(tmp_path).start()
            real = Downloader(rig.store, rig.registry, tmp_path / "artifacts",
                              proxy_url=hz.download_url,
                              ca_file=tmp_path / "ca" / "mitmproxy-ca-cert.pem")
            real.user_agent = rig.downloader.user_agent
            rig.ctl.downloader = real
            run = await rig.run()
            active = rig.ctl.active
            assert active.ref.kind == "account"
            context = rig.browser.session.context
            context.jar.append({"name": "sess", "value": "SiteSession123", "domain": "a.example",
                                "path": "/", "expires": -1})
            asked = []
            original = context.cookies

            async def counting(urls=None):
                asked.append(urls)
                return await original(urls)
            context.cookies = counting
            hz.addon.install(active.policy)
            hz.fixture.pages["/account"] = ("text/plain", b"public text", {})
            result = await rig.ctl.fetch(run.run_id, "https://a.example/account")
            receipts = hz.fixture.receipts
            state = ([r[1:3] for r in receipts.requests], list(receipts.cookies), asked)
            await rig.ctl.shutdown()
            rig.close()
            return result, state

    result, (requests, cookies, asked) = h.run(go)
    assert result.outcome == "done" and result.extra["text"] == "public text"
    assert requests == [("a.example", "/account")]
    assert cookies == [] and asked == []


# ------------------------------------------------------------------ reading


def test_html_reads_as_plain_text_with_its_links_inline():
    assert html_text(PAGE) == ("Opening hours\nHours\nOpen daily from nine. More (/more?x=1)\n"
                               "One\nTwo top")


def test_html_survives_what_the_web_serves():
    assert html_text(b"<p>one<script>var a='<p>no</p>'") == "one"
    assert html_text(b"</a></script><a href=x>y") == "y"     # unbalanced, unclosed
    assert html_text("caf\xe9 <b>ok</b>".encode("latin-1")) == "caf� ok"


DATA = (b'{"object":"list","total":2,"data":['
        b'{"name":"First","prices":{"eur":"1.50"},"id":"a"},'
        b'{"name":"Second","id":"b"},7],"meta":{"page":1}}')


@pytest.mark.parametrize("fields, expected", [
    (("total",), '{"total": 2}'),
    (("data.name",), '{"data": [{"name": "First"}, {"name": "Second"}]}'),
    (("data.name", "data.prices.eur"),     # a value stays beside its neighbours
     '{"data": [{"name": "First", "prices": {"eur": "1.50"}}, {"name": "Second"}]}'),
    (("missing", "meta.nope", "data.nope"), "{}"),
    (("meta",), '{"meta": {"page": 1}}'),
])
def test_fields_keep_the_shape_and_a_missing_path_is_absent(fields, expected):
    assert pick_fields(DATA, fields) == expected


def test_fields_on_a_list_at_the_top_and_on_what_is_not_json():
    assert pick_fields(b'[{"a":1,"b":2},{"a":3}]', ("a",)) == '[{"a": 1}, {"a": 3}]'
    assert pick_fields(b'{"list":[]}', ("list.a",)) == '{"list": []}'
    assert pick_fields(b"<html>no</html>", ("a",)) is None
    assert pick_fields(b"[" * 200_000, ("a",)) is None       # nested past the parser


def test_parse_fetch():
    assert parse_fetch({"url": "https://a.example/"}) == ("https://a.example/", ())
    assert parse_fetch({"url": "https://a.example/", "fields": ["a.b"]})[1] == ("a.b",)
    for bad in ({}, {"url": ""}, {"url": "u", "fields": []}, {"url": "u", "fields": ["a"] * 51},
                {"url": "u", "fields": ["a" * 201]}, {"url": "u", "x": 1}, {"url": "u" * 2049}):
        with pytest.raises(ProtocolError):
            parse_fetch(bad)


# ------------------------------------------------------------------ the controller


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.ctl.shutdown()
    r.close()


def _moved(rig, mark):
    """What a browser, a proxy or a page was asked since ``mark``, a plain fetch's record apart."""
    return [e for e in rig.log[mark:] if e[0] != "fetch_plain"]


async def test_a_fetch_moves_neither_chrome_nor_the_session_nor_the_policy(rig):
    run = await rig.run()
    active, session = rig.ctl.active, rig.browser.session
    page = session.page
    url, revision, policy = page.url, active.revision, active.policy
    generation = session.view_generation
    evaluations = []
    original = page.evaluate

    async def counting(js, *args):
        evaluations.append(js)
        return await original(js, *args)
    page.evaluate = counting
    mark = len(rig.log)
    rig.downloader.plain = (PAGE, "text/html")
    result = await rig.ctl.fetch(run.run_id, "https://b.example/hours")
    assert result.outcome == "done"
    assert _moved(rig, mark) == []           # no open, close, exit proof, install, goto, status
    assert rig.browser.session is session and active.session is session and page.url == url
    assert active.revision == revision and active.policy is policy
    assert session.view_generation == generation and evaluations == []
    assert rig.log[mark:] == [("fetch_plain", "https://b.example/hours", "Mozilla/5.0 synthetic")]


async def test_a_fetch_with_no_live_session_does_not_reopen_chrome(rig):
    """A forced close left no session; the User-Agent kept while a page existed still serves."""
    rig.driver.close_modes = {"login": "forced"}
    run = await rig.run()
    assert rig.browser.session is None and rig.state(run.run_id) == "running"
    mark = len(rig.log)
    rig.downloader.plain = (b"hello", "text/plain")
    result = await rig.ctl.fetch(run.run_id, "https://b.example/hours")
    assert result.outcome == "done" and result.extra["text"] == "hello"
    assert _moved(rig, mark) == [] and rig.browser.session is None
    assert rig.ctl.active.session is None and rig.state(run.run_id) == "running"


async def test_a_user_agent_never_read_falls_back_to_one_realistic_string(rig):
    async def fails(session):
        raise RuntimeError("no page")
    rig.downloader.user_agent = fails
    run = await rig.run()
    assert rig.state(run.run_id) == "running"
    rig.downloader.plain = (b"hello", "text/plain")
    mark = len(rig.log)
    assert (await rig.ctl.fetch(run.run_id, "https://b.example/hours")).outcome == "done"
    assert rig.log[mark:] == [("fetch_plain", "https://b.example/hours", FALLBACK_USER_AGENT)]
    assert "Chrome/" in FALLBACK_USER_AGENT and "Headless" not in FALLBACK_USER_AGENT


async def test_a_later_session_refreshes_the_agent_and_a_failed_read_keeps_the_last(rig):
    run = await rig.run()
    assert rig.ctl.active.user_agent == "Mozilla/5.0 synthetic"

    async def fails(session):
        raise RuntimeError("no page")
    rig.downloader.user_agent = fails
    await rig.ctl.search(run.run_id, "weather")       # a switch to the public browser
    assert rig.browser.session.kind == "public"
    assert rig.ctl.active.user_agent == "Mozilla/5.0 synthetic"


@pytest.mark.parametrize("address", ["http://b.example/", "not an address", "https://",
                                     "https://b.example:bad/", "https://b.example/a b"])
async def test_an_address_start_url_refuses_is_said_and_never_sent(rig, address):
    run = await rig.run()
    mark = len(rig.log)
    result = await rig.ctl.fetch(run.run_id, address)
    assert (result.outcome, result.extra["reason"]) == ("not_run", "bad_address")
    assert rig.log[mark:] == []


async def test_during_a_question_a_fetch_waits_like_any_step(rig):
    run = await rig.run()
    assert rig.store.transition(run.run_id, "running", "waiting_approval")
    mark = len(rig.log)
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.fetch(run.run_id, "https://b.example/hours")
    assert rig.log[mark:] == [] and rig.state(run.run_id) == "waiting_approval"


async def test_a_fetch_leaves_a_frozen_judgement_standing(rig):
    run = await rig.run()
    frozen = object()
    rig.ctl.active.pending = frozen
    rig.downloader.plain = (b"hello", "text/plain")
    await rig.ctl.fetch(run.run_id, "https://b.example/hours")
    assert rig.ctl.active.pending is frozen
    rig.ctl.active.pending = None


async def test_a_fetch_holds_the_command_lock_like_a_download(rig):
    run = await rig.run()
    rig.downloader.plain = (b"hello", "text/plain")
    rig.downloader.gate = asyncio.Event()
    task = asyncio.create_task(rig.ctl.fetch(run.run_id, "https://b.example/hours"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert rig.ctl._lock.locked()
    rig.downloader.gate.set()
    assert (await task).outcome == "done"
    assert not rig.ctl._lock.locked()


@pytest.mark.parametrize("reason", ["http_403", "http_404", "blocked", "not_document"])
async def test_a_refusal_is_a_step_reply_and_no_source(rig, reason):
    run = await rig.run()
    rig.downloader.plain = PlainAnswer(reason)
    result = await rig.ctl.fetch(run.run_id, "https://b.example/hours")
    assert result.to_wire() == {"outcome": "not_run", "generation": result.generation,
                                "reason": reason}
    assert rig.store.sources_for(run.run_id) == []
    assert rig.state(run.run_id) == "running"


async def test_the_reply_is_the_first_window_and_run_read_continues(rig):
    run = await rig.run()
    body = "".join(f"line {i}\n" for i in range(5_000))
    rig.downloader.plain = (body.encode(), "text/plain")
    first = (await rig.ctl.fetch(run.run_id, "https://b.example/long")).extra
    assert first["file"]["mime"] == "text/plain" and "private_path" not in first["file"]
    assert first["text"] == body[:MAX_READ] and first["next_offset"] == MAX_READ
    assert first["complete"] is False
    second = await rig.ctl.read(run.run_id, None, first["file"]["file_id"], first["next_offset"])
    assert first["text"] + second["text"] == body and second["complete"] is True


async def test_html_is_read_as_text_in_the_reply_and_by_run_read(rig):
    run = await rig.run()
    rig.downloader.plain = (PAGE, "text/html")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/hours")).extra
    assert extra["text"] == html_text(PAGE, "https://b.example/hours") and "dropped" not in extra["text"]
    again = await rig.ctl.read(run.run_id, None, extra["file"]["file_id"], 0)
    assert again["text"] == extra["text"] and again["complete"] is True


async def test_masking_applies_to_the_window_and_to_fields(rig):
    run = await rig.run()
    rig.downloader.plain = (f'{{"key":"{TOKEN}","n":1}}'.encode(), "application/json")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/a")).extra
    assert TOKEN not in extra["text"] and extra["masked"] is True
    picked = (await rig.ctl.fetch(run.run_id, "https://b.example/a", ("key",))).extra
    assert TOKEN not in picked["text"] and picked["masked"] is True


async def test_fields_hold_only_those_and_say_when_they_cannot(rig):
    run = await rig.run()
    rig.downloader.plain = (DATA, "application/json")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/a", ("data.name",))).extra
    assert extra["text"] == '{"data": [{"name": "First"}, {"name": "Second"}]}'
    assert extra["complete"] is True and "not_json" not in extra
    rig.downloader.plain = (PAGE, "text/html")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/p", ("data.name",))).extra
    assert extra["not_json"] is True and extra["text"] == html_text(PAGE, "https://b.example/p")


async def test_fields_past_one_window_are_not_complete(rig):
    run = await rig.run()
    big = b'{"a":"' + b"word " * 10_000 + b'"}'
    rig.downloader.plain = (big, "application/json")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/a", ("a",))).extra
    assert len(extra["text"]) == MAX_READ
    assert extra["complete"] is False and extra["next_offset"] is None


async def test_a_type_that_is_not_text_comes_back_with_its_id_and_type(rig):
    run = await rig.run()
    rig.downloader.plain = (b"%PDF-1.7 synthetic", "application/pdf")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/a.pdf", ("a",))).extra
    assert extra["text"] is None and extra["file"]["mime"] == "application/pdf"
    assert extra["file"]["file_id"] and extra["not_json"] is True


@pytest.mark.parametrize("mode", ["account", "open_web"])
async def test_the_final_address_becomes_a_source_in_any_run(rig, mode):
    """In an account run, signed-in browser open, too: the record holds the address that served
    the page and its title; the reply holds only the id."""
    s = spec() if mode == "account" else spec(request_id="ow", mode="open_web", sites=())
    run = await rig.run(s)
    final = "https://www.b.example/hours?lang=en"
    rig.downloader.plain, rig.downloader.plain_url = (PAGE, "text/html"), final
    result = await rig.ctl.fetch(run.run_id, "https://b.example/hours")
    assert [(x.source_id, x.url, x.title_masked) for x in rig.store.sources_for(run.run_id)] == [
        (result.extra["source_id"], final, "Opening hours")]
    assert result.extra["url"] == final      # the model reads it, to resolve relative links


@pytest.mark.parametrize("final", ["http://b.example/plain", "https://b.example/" + "a" * 900])
async def test_an_address_that_cannot_be_a_source_gets_none(rig, final):
    run = await rig.run()
    rig.downloader.plain, rig.downloader.plain_url = (b"hello", "text/plain"), final
    result = await rig.ctl.fetch(run.run_id, "https://b.example/x")
    assert result.outcome == "done" and "source_id" not in result.extra
    assert rig.store.sources_for(run.run_id) == []


async def test_a_downloaded_page_also_reads_as_text(rig):
    run = await rig.run()
    rig.page().show("https://a.example/start", "Start")
    record = rig.downloader._record(rig.store.get_run(run.run_id), None, PAGE)
    rig.store.put_file(replace(record, file_id="fhtml", mime="text/html"))
    assert (await rig.ctl.read(run.run_id, None, "fhtml", 0))["text"] == html_text(PAGE)


# ------------------------------------------------------------------ addresses


LINKS = (b'<!doctype html><title>Links</title><p><a href="https://other.example/2024/page_two-3">'
         b'Next</a></p>')
WHOLE = "https://other.example/2024/page_two-3"


async def test_a_plain_read_keeps_addresses_in_its_reply_and_in_run_read(rig):
    run = await rig.run()
    rig.downloader.plain = (LINKS, "text/html")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/l")).extra
    assert f"Next ({WHOLE})" in extra["text"]
    again = await rig.ctl.read(run.run_id, None, extra["file"]["file_id"], 0)
    assert f"Next ({WHOLE})" in again["text"]


async def test_fields_of_a_plain_read_keep_addresses(rig):
    run = await rig.run()
    next_page = "https://api.b.example/items?page=2&q=one%20two"
    rig.downloader.plain = (json.dumps({"next_page": next_page, "n": 1}).encode(),
                            "application/json")
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/j", ("next_page",))).extra
    assert json.loads(extra["text"]) == {"next_page": next_page}


async def test_a_file_downloaded_from_a_page_stays_fully_masked(rig):
    """The signed-in session's page especially: its addresses can carry what only he can see."""
    run = await rig.run()
    rig.page().show("https://a.example/start", "Start")
    record = rig.downloader._record(rig.store.get_run(run.run_id), None, LINKS)
    rig.store.put_file(replace(record, file_id="fpage", mime="text/html"))
    text = (await rig.ctl.read(run.run_id, None, "fpage", 0))["text"]
    assert WHOLE not in text and "https://other.example ⟨r1⟩" in text


async def test_only_this_files_id_is_kept_whole(rig):
    """A plain fetch beside a page's download changes nothing for the download."""
    run = await rig.run()
    rig.downloader.plain = (LINKS, "text/html")
    await rig.ctl.fetch(run.run_id, "https://b.example/l")
    record = rig.downloader._record(rig.store.get_run(run.run_id), None, LINKS)
    rig.store.put_file(replace(record, file_id="fpage", mime="text/html"))
    assert WHOLE not in (await rig.ctl.read(run.run_id, None, "fpage", 0))["text"]


async def test_a_secret_shaped_token_in_a_kept_address_is_masked_in_place(rig):
    run = await rig.run()
    final = f"https://www.b.example/a/{TOKEN}/b?sig={TOKEN}&page=2"
    shown = "https://www.b.example/a/[masked]/b?sig=[masked]&page=2"
    body = f'<title>T</title><a href="{final}">Next</a>'.encode()
    rig.downloader.plain, rig.downloader.plain_url = (body, "text/html"), final
    extra = (await rig.ctl.fetch(run.run_id, "https://b.example/l")).extra
    assert f"Next ({shown})" in extra["text"] and extra["masked"] is True
    assert extra["url"] == shown
    assert [s.url for s in rig.store.sources_for(run.run_id)] == [final]   # the operator's record


def test_keep_addresses_leaves_the_other_masking_on():
    key = "ghp_" + "a1B2" * 10
    text = f"key {key} word {TOKEN} https://a.example/x/{key}"
    got = mask_whole(text, keep_addresses=True)[0]
    assert got == "key [masked] word [masked] https://a.example/x/[masked]"
    assert mask_whole(text)[0] != got and "https://a.example ⟨r1⟩" in mask_whole(text)[0]


def test_links_resolve_whole_from_the_address_that_answered_and_the_first_base():
    at = "https://public.example/a/b/page"
    assert "N (https://public.example/a/b/next)" in html_text(b'<a href="next">N</a>', at)
    assert "T (https://public.example/top)" in html_text(b'<a href="/top">T</a>', at)
    assert "N (https://public.example/next)" in html_text(
        b'<base href="../assets/"><a href="../../next">N</a>', at)
    assert "N (https://a.example/docs/next)" in html_text(
        b'<base href="https://a.example/docs/"><base href="https://b.example/">'
        b'<a href="next">N</a>', at)
    assert "C (https://c.example/x)" in html_text(b'<a href="https://c.example/x">C</a>', at)
    assert html_text(b'<a href="next">N</a>') == "N (next)"     # no address known: as written


def test_a_malformed_base_or_link_never_costs_the_page():
    at = "https://public.example/a/"
    assert "N (https://public.example/a/x)" in html_text(
        b'<base href="https://[bad/"><a href="x">N</a>', at)
    assert "B (https://[bad/)" in html_text(b'<a href="https://[bad/">B</a>', at)


def test_a_template_is_inert_and_a_base_cannot_multiply_a_page():
    at = "https://public.example/a/"
    assert html_text(b'<template><base href="https://x.example/"><a href="y">Z</a></template>'
                     b'<a href="n">N</a>', at) == "N (https://public.example/a/n)"
    big = b'<base href="https://public.example/' + b"p" * 100_000 + b'/">' + b'<a href=x>x</a>' * 2000
    assert len(html_text(big, at)) < 200_000


def test_a_json_answer_too_deep_to_narrow_reads_as_its_text():
    assert pick_fields(b"[" * 500 + b'{"a":1}' + b"]" * 500, ("a",)) is None
