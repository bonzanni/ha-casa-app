"""Task 14 Steps 1 and 5 in the image: the production run controller driving branded Chrome
through the enforcing proxy (the test harness on the production port) against synthetic
adversarial sites. Every unsafe case asserts, at the fixture's socket relay, that no
unintended request went upstream, and, over everything a model or a log consumer could see
(the replies, the step log, the records, the notices, the controller's log), that no secret,
cookie, raw path, query or attribute escaped. Fixtures are generic and synthetic, never a
per-site adapter.

* a noisy account page: secret text and attributes, prefilled and hidden fields, a cookie, a
  bearer link, a cross-origin iframe, a beacon, analytics and GraphQL sends, a worker's send, a
  WebSocket, a client-side route change, and a navigation off the site (refused);
* a signed-in site's own sends (statements, GraphQL operations): each checked press sends once;
* WebSockets to the site and to a third party open;
* an account + public task: a redirect download, a third-party redirect refused, a zip of the
  run's files, a third registered site refused, records scoped to the requester;
* an open-web run: blank, a search, a result page, a download, its sources exactly the
  ``https`` pages it loaded, the account profiles untouched;
* a broker login with password and TOTP, then forget — no vault item deleted;
* a password-change page met during a login (no automatic reset: ``login_needed``, nothing
  filled) and a login redirected to an unregistered provider (no credential leaves);
* the broker killed during a login, the controller killed during a download;
* Chrome's own background sends: refused quietly, never a question.

**Every refusal or absence assertion has a positive control on the same target** (operator
ruling E-T14-1 = O1): an unsafe send was attempted and the proxy refused it (its own event
ring, ``_refused_attempts``/``_refused_counts``); a redirect's first hop was fetched; a cookie
went back to its own site before it is shown not to reach another; the same journey in a
second run fetched the same pages; a profile change is seen by the listing that shows none;
the secret scans (``scanned``) cover every model-route reply, the step log, the records, the
notices and every output stream — the controller's logging file, stdout and stderr, the test
broker's stdout and stderr, the harness proxy's records through the production filter — and
find a canary
planted on each stream before they look for secrets (``run-integration.sh`` does the same for
the container's own log); a killed login's next step is reached by a new run once the broker
is back.

**Known test limits** (operator ruling E-T14-2 = P3: documented, not fixed): the controller
killed during a download dies before any partial file exists, and the check after the restart
reads the files directory only, not the store's file records.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from lifecycleprobe import controller_home, run_lifecycle
from sitefixture import ChromeSites
from test_run_lifecycle import _chrome_processes, _controller_uid, fixture_config, form

pytestmark = pytest.mark.docker

A, B, C = "app.site-a.example", "pub.site-b.example", "app.site-c.example"
FRAME, TRACK, IDP = "frame.other.example", "analytics.track.example", "idp.provider.example"
LOGIN = "login.site-a.example"
SEARCH = "www.google.com"
PDF = b"%PDF-1.7\n% synthetic\n" + b"0" * 1500 + b"\n%%EOF\n"


def token() -> str:
    """A canary in a credential-like shape (masked when it is page text). Its fixed prefix lets
    the lane scan the app container's own log (``docker logs``) for any of them."""
    return "itsecret" + secrets.token_hex(16)


def site_step(site_id, host, start="/home", login=(), item=None, name=None):
    return {"site": {"site_id": site_id, "name": name or site_id, "origins": [f"https://{host}"],
                     "login": list(login), "item": item, "start": f"https://{host}{start}"}}


def scan(blob: str, *needles) -> list[str]:
    return [n for n in needles if n in blob]


def scanned(steps, *, home: Path, sites, broker_fixture=None, **kw):
    """Run ``steps`` and return ``(out, blob)``: ``blob`` is every model-route reply, event,
    record and notice (the scenario's ``surfaces`` step, when it has one) plus every log stream
    of the run — the controller's logging file and stderr, the test broker's stderr, the
    harness proxy's log records. A canary planted on each stream is found first (the positive
    control that the scan covers it); the caller then scans ``blob`` for its secrets."""
    import logging
    marker = "itplanted" + secrets.token_hex(8)
    if broker_fixture is not None:
        broker_fixture = {**broker_fixture, "log_marker": marker}
    logs: dict = {}
    out = run_lifecycle([*steps, {"plant_log": {"marker": marker}}], home=home,
                        broker_fixture=broker_fixture, logs=logs, **kw)
    logging.getLogger("casa_browser.proxy_addon").warning("planted %s", marker)
    streams = {"controller log file": (home / "controller.log").read_text(errors="replace"),
               "harness proxy log": "\n".join(sites.log_records), **logs}
    assert {"controller stdout", "controller stderr"} <= set(streams)
    if broker_fixture is not None:
        assert {"broker stdout", "broker stderr"} <= set(streams)
    for name, text in streams.items():
        assert marker in text, f"the planted canary is not in the {name}: the scan misses it"
    surfaces = next((o for o in out if isinstance(o, dict) and "replies" in o), {})
    return out[:-1], json.dumps(surfaces) + "\n" + "\n".join(streams.values())


def hosts_reached(sites) -> set[str]:
    return {r[1].split(":")[0] for r in sites.receipts.requests}


def _refused_counts(sites):
    """(method, host) of every refused request, counted."""
    from collections import Counter
    return Counter(_refused_list(sites))


def _refused_attempts(sites) -> set[tuple[str, str]]:
    return set(_refused_list(sites))


def _refused_list(sites) -> list[tuple[str, str]]:
    """(method, host) of every request the proxy refused, from its own event ring (the
    positive control that an attempt was made and reached the proxy; an event names the
    origin, never the path)."""
    from urllib.parse import urlsplit
    return [(e.method, urlsplit(e.origin).hostname if e.origin else None)
            for e in sites.events() if not e.allow]


# --------------------------------------------------------------------------- noisy account page


def test_noisy_account_page_admits_nothing_off_the_site_and_shows_no_secret(tmp_path):
    """Design §4 on a noisy account page: the page's own traffic is free — its sends, a
    worker's, a beacon to a third party, a cross-origin frame, a WebSocket — with the
    account cookie on its own site's requests only; a top-level document off the site is
    refused before any byte (``site_lock``); and nothing secret reaches a model or a log."""
    secret_text, secret_query, secret_link, secret_value = token(), token(), token(), token()
    secret_hidden, secret_attr, secret_title, secret_cookie, secret_beacon = (
        token(), token(), token(), token(), token())
    secret_leave = token()
    visible = "Visible" + secrets.token_hex(3)   # short, plain page text: shown, so the scan must find it
    page = f"""<html><head><title>Account home</title></head><body>
      <p>Statement ready. Your key {secret_text}. Reference {visible}</p>
      <p>Details at https://{A}/private/path?session={secret_query} today</p>
      <a href="https://{A}/doc?token={secret_link}">Statement link</a>
      <a href="https://{A}/doc2" target="_blank">Open in a new tab</a>
      <input name="q" value="{secret_value}" aria-label="Search box">
      <input type="hidden" name="csrf" value="{secret_hidden}">
      <div data-secret="{secret_attr}" title="{secret_title}">Note</div>
      <iframe src="https://{FRAME}/embed" width="200" height="100"></iframe>
      <button id="next" onclick="history.pushState({{}}, '', '/home/route')">Next</button>
      <button id="leave" onclick="location.href = 'https://{TRACK}/landing?k={secret_leave}'">Leave</button>
      <script>
        setTimeout(() => {{
          navigator.sendBeacon('https://{TRACK}/collect', 'e={secret_beacon}');
          fetch('/collect', {{method: 'POST', body: 'e=1'}}).catch(() => 0);
          fetch('/graphql', {{method: 'POST', headers: {{'content-type': 'application/json'}},
                body: JSON.stringify({{operationName: 'Me', query: 'query Me {{ me {{ id }} }}'}})}}).catch(() => 0);
          try {{ new Worker('/w.js'); }} catch (e) {{}}
          try {{ const ws = new WebSocket('wss://{A}/ws'); ws.onopen = () => ws.send('hi'); }} catch (e) {{}}
        }}, 200);
      </script></body></html>""".encode()
    home = controller_home(tmp_path)
    with ChromeSites([A, FRAME, TRACK]) as sites:
        _controller_uid(sites)
        sites.pages["/home"] = ("text/html", page, {"Set-Cookie": f"sid={secret_cookie}; Secure; Path=/"})
        sites.pages["/w.js"] = ("text/javascript",
                                b"fetch('/worker-post', {method: 'POST', body: 'w=1'}).catch(() => 0);", {})
        steps = [
            site_step("sa", A),
            {"run": {"name": "r", "request_id": "it-noise", "sites": [["sa", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"sleep": 3},
            {"observe": {"name": "r"}},                                      # 4
            {"act": {"name": "r", "kind": "click", "label": "Next"}},
            {"observe": {"name": "r"}},                                      # 6
            {"act": {"name": "r", "kind": "click", "label": "Leave"}},       # 7
            {"sleep": 2},
            {"observe": {"name": "r"}},                                      # 9
            {"questions": {"name": "r"}},                                      # 10
            {"finish": {"name": "r"}},                                       # 11
            {"surfaces": {"name": "r"}},
        ]
        out, blob = scanned(steps, home=home, sites=sites, broker_fixture=fixture_config(tmp_path))
        obs = out[4]
        assert "Statement ready" in obs["text"], obs
        assert "Statement link" in obs["labels"] and "Next" in obs["labels"]
        # the client-side route change stays on the site's origin: the session's page
        assert "Statement ready" in out[6].get("text", ""), out[6]
        # the page left the site: its document was refused, so there is no page to read
        assert out[7].get("outcome") == "done", out[7]
        assert out[9] == {"error": "blocked"}, out[9]
        assert out[10] == [], out[10]                  # no request is a question any more
        received = {(r[0], r[1].split(":")[0], r[2].split("?")[0]) for r in sites.receipts.requests}
        # the page's own traffic reached its destinations: the site's sends, the worker's,
        # the third party's beacon and the cross-origin frame
        for want in (("POST", A, "/collect"), ("POST", A, "/graphql"), ("POST", A, "/worker-post"),
                     ("POST", TRACK, "/collect"), ("GET", FRAME, "/embed")):
            assert want in received, (want, sorted(received))
        # a WebSocket to a floor-passing origin opens
        assert ("WS", A, "/ws") in received and sites.receipts.ws_messages == ["hi"]
        # the top-level document off the site: refused at the proxy, never upstream
        assert not [r for r in sites.receipts.requests if r[2].startswith("/landing")]
        left = [e for e in sites.events() if not e.allow and e.origin == f"https://{TRACK}"]
        assert left and {(e.reason, e.resource_type) for e in left} == {("site_lock", "document")}, left
        # the account cookie went back to its own site (the positive control) and to nothing else
        cookies = sites.receipts.cookies
        assert [c for c in cookies if c[0].split(":")[0] == A and secret_cookie in c[2]], cookies
        assert not [c for c in cookies if c[0].split(":")[0] != A], cookies
        assert visible in blob        # positive control: the scan covers the replies the model got
        leaked = scan(blob, secret_text, secret_query, secret_link, secret_value, secret_hidden,
                      secret_attr, secret_title, secret_cookie, secret_beacon, secret_leave)
        assert not leaked, leaked
        for raw in ("/private/path", "session=", "token=", "/collect", "/graphql", "/worker-post",
                    "/home/route", "/doc2", "/landing", f"https://{A}/", FRAME + "/embed",
                    TRACK + "/collect"):
            assert raw not in blob, raw
        assert out[11] == "completed"
    assert not _chrome_processes()


# --------------------------------------------------------------------------- the site's own sends


STATEMENTS = """<html><head><title>Statements</title></head><body><p>Statements</p>
<p id="out">none</p>
<script>
function send(body, type) {
  fetch('/statement', {method: 'POST', headers: {'content-type': type}, body})
    .then(r => r.text()).then(t => { document.getElementById('out').textContent = t; }).catch(() => 0);
}
function statement(account, month, format) {
  send(new URLSearchParams({account, month, format}).toString(), 'application/x-www-form-urlencoded');
}
function gql(op, query, variables) {
  fetch('/graphql', {method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({operationName: op, query, variables})}).catch(() => 0);
}
const Q = 'query Statement($m: String!) { statement(month: $m) { id } }';
</script>
<button onclick="statement('acc-1', '2026-09', 'pdf')">Generate September</button>
<button onclick="statement('acc-1', '2026-08', 'pdf')">Generate August</button>
<button onclick="statement('acc-2', '2026-09', 'pdf')">Generate other account</button>
<button onclick="statement('acc-1', '2026-09', 'csv')">Generate CSV</button>
<button onclick="gql('Statement', Q, {m: '2026-09'})">Query September</button>
<button onclick="gql('Statement', Q, {m: '2026-08'})">Query August</button>
<button onclick="gql('Delete', 'mutation Delete { drop { id } }', {})">Query other</button>
</body></html>""".encode()


def _statement_posts(sites):
    return [r[3].decode() for r in sites.receipts.requests if r[0] == "POST" and r[2] == "/statement"]


def _graphql_posts(sites):
    return [json.loads(r[3]) for r in sites.receipts.requests if r[0] == "POST" and r[2] == "/graphql"]


def test_a_signed_in_sites_own_sends_go_once_each_and_show_nothing(tmp_path):
    """Design §4: on a signed-in site the page's own sends are free — each checked
    press (a scripted ``go``) sends its request exactly once, the changed export format and
    another GraphQL operation included — and no address, body or field value reaches a
    model-facing reply or a log."""
    def click(label):
        return [{"act": {"name": "r", "kind": "click", "label": label}}, {"sleep": 1.5}]

    home = controller_home(tmp_path)
    with ChromeSites([A]) as sites:
        _controller_uid(sites)
        sites.pages["/statements"] = ("text/html", STATEMENTS, {})
        sites.pages["/statement"] = ("text/plain", b"generated", {})
        sites.pages["/graphql"] = ("application/json", b'{"data": {}}', {})
        steps = [
            site_step("sa", A, start="/statements"),
            {"run": {"name": "r", "request_id": "it-gen", "sites": [["sa", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},                                      # 3
            *click("Generate September"),
            *click("Generate August"),
            *click("Generate other account"),
            *click("Generate CSV"),
            *click("Query September"),
            *click("Query August"),
            *click("Query other"),
            {"questions": {"name": "r"}},                                      # 18
            {"finish": {"name": "r"}},
            {"surfaces": {"name": "r"}},
        ]
        out, blob = scanned(steps, home=home, sites=sites, broker_fixture=fixture_config(tmp_path))
        assert "Statements" in out[3]["text"], out[3]
        assert [out[i].get("outcome") for i in range(4, 18, 2)] == ["done"] * 7, out[4:18]
        assert _statement_posts(sites) == ["account=acc-1&month=2026-09&format=pdf",
                                           "account=acc-1&month=2026-08&format=pdf",
                                           "account=acc-2&month=2026-09&format=pdf",
                                           "account=acc-1&month=2026-09&format=csv"]
        queries = _graphql_posts(sites)
        assert [(q["operationName"], q["variables"]) for q in queries] == [
            ("Statement", {"m": "2026-09"}), ("Statement", {"m": "2026-08"}), ("Delete", {})], queries
        assert out[18] == [] and not [x for x in _refused_list(sites) if x[1] == A], _refused_list(sites)
        assert "Generate September" in blob     # positive control: the replies are scanned
        for raw in ("/statement", "/graphql", "acc-1", "acc-2", "2026-09"):
            assert raw not in blob, raw
    assert not _chrome_processes()


# --------------------------------------------------------------------------- WebSocket


def test_websocket_denied_allowed_then_revoked(tmp_path):
    """Design §4: an account page's WebSockets are free — one to the site and one to
    another floor-passing origin both open and carry their frames — and a socket to a
    private address is refused by the floor."""
    page = f"""<html><head><title>Live</title></head><body><p>Live prices</p><p id="s">idle</p>
    <script>function go(url, id) {{ const ws = new WebSocket(url);
      ws.onopen = () => {{ ws.send('subscribe ' + id); document.getElementById('s').textContent += ' open-' + id; }};
      ws.onerror = () => {{ document.getElementById('s').textContent += ' refused-' + id; }}; }}</script>
    <button onclick="go('wss://{A}/feed', 'site')">Connect</button>
    <button onclick="go('wss://{TRACK}/feed', 'other')">Connect other</button>
    <button onclick="go('wss://router.lan/feed', 'lan')">Connect router</button></body></html>""".encode()

    def connect(label):
        return [{"act": {"name": "r", "kind": "click", "label": label}}, {"sleep": 2}]

    home = controller_home(tmp_path)
    with ChromeSites([A, TRACK]) as sites:
        _controller_uid(sites)
        sites.pages["/live"] = ("text/html", page, {})
        steps = [
            site_step("sa", A, start="/live"),
            {"run": {"name": "r", "request_id": "it-ws", "sites": [["sa", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
            *connect("Connect"),                                             # 4, 5
            *connect("Connect other"),                                       # 6, 7
            *connect("Connect router"),                                      # 8, 9
            {"js": {"name": "r", "code": "() => document.getElementById('s').textContent"}},  # 10
            {"questions": {"name": "r"}},                                      # 11
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=home, broker_fixture=fixture_config(tmp_path))
        sockets = sorted((r[1].split(":")[0], r[2]) for r in sites.receipts.requests if r[0] == "WS")
        assert sockets == sorted([(A, "/feed"), (TRACK, "/feed")]), sites.receipts.requests
        assert sorted(sites.receipts.ws_messages) == ["subscribe other", "subscribe site"]
        assert "open-site" in out[10] and "open-other" in out[10] and "open-lan" not in out[10], out[10]
        ws = [e for e in sites.events() if e.resource_type == "websocket"]
        assert {e.reason for e in ws if e.allow} == {"site_page"}, [(e.allow, e.reason) for e in ws]
        assert out[11] == [], out[11]
    assert not _chrome_processes()


# --------------------------------------------------------------------------- account + public


def test_multisite_zip_third_site_public_and_records_scoped(tmp_path):
    """An account + public task: a redirect download and one whose redirect leaves for an
    unregistered host (the downloader's hops meet the floor only, design §4), a zip of the
    run's files, a registered site outside the task's set (opened as a public site, with no
    cookie of any account profile), records scoped to the requester."""
    config = fixture_config(tmp_path)
    home = controller_home(tmp_path)
    with ChromeSites([A, B, C, TRACK]) as sites:
        _controller_uid(sites)
        sites.pages["/home"] = ("text/html", b"<title>Account</title><p>Account home</p>"
                                b'<img src="/pixel.png" alt="">'
                                b'<a href="https://pub.site-b.example/docs">Public docs</a>',
                                {"Set-Cookie": "acct=AcctSession42; Secure; Path=/"})
        sites.pages["/pixel.png"] = ("image/png", b"\x89PNG\r\n\x1a\n", {})
        sites.pages["/docs"] = ("text/html", (
            b"<title>Docs</title><p>Public documents</p>"
            b'<a href="https://pub.site-b.example/dl">Report PDF</a> '
            b'<a href="https://pub.site-b.example/away">Elsewhere PDF</a> '
            b'<a href="https://app.site-c.example/home">Site C</a>'), {})
        sites.pages["/dl"] = ("text/html", b"", {"Location": "https://pub.site-b.example/report.pdf"})
        sites.status["/dl"] = 302
        sites.pages["/away"] = ("text/html", b"", {"Location": f"https://{TRACK}/x.pdf"})
        sites.status["/away"] = 302
        sites.pages["/report.pdf"] = ("application/pdf", PDF, {})
        sites.pages["/x.pdf"] = ("application/pdf", PDF, {})
        steps = [
            site_step("sa", A, name="Site A"),
            site_step("sb", B, start="/docs", name="Public B"),
            site_step("sc", C, name="Site C"),
            {"run": {"name": "r", "request_id": "it-multi", "requester": "alice",
                     "sites": [["sa", "account"], ["sb", "public"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "navigate", "label": "Open site: Public B"}},
            {"observe": {"name": "r"}},
            {"download": {"name": "r", "label": "Report PDF"}},              # 8
            {"download": {"name": "r", "label": "Elsewhere PDF"}},           # 9
            {"act": {"name": "r", "kind": "click", "label": "Site C"}},      # 10
            {"observe": {"name": "r"}},                                      # 11
            {"archive": {"name": "r"}},                                      # 12
            {"finish": {"name": "r", "file_ids": []}},
            {"records": {"requester": "alice"}},                             # 14
            {"records": {"requester": "mallory"}},                           # 15
        ]
        out = run_lifecycle(steps, home=home, broker_fixture=config)
        assert out[8]["head"] == "%PDF-" and out[8]["size"] == len(PDF), out[8]
        # the redirect to an unregistered host: both hops sent, the file arrives (a PDF is a
        # document, kept with no check)
        assert out[9]["head"] == "%PDF-" and out[9]["size"] == len(PDF), out[9]
        hops = [(r[0], r[1].split(":")[0], r[2]) for r in sites.receipts.requests]
        assert ("GET", B, "/away") in hops and ("GET", TRACK, "/x.pdf") in hops, hops
        assert out[12]["mime"] == "application/zip" and len(out[12]["names"]) == 2, out[12]
        # a registered site outside the task's set is a public site for this run: its page is
        # opened in the public profile and read there
        assert out[10].get("outcome") == "done", out[10]
        assert "Account home" in out[11].get("text", ""), out[11]
        assert ("GET", C, "/home") in hops, hops
        assert len(out[14]) == 1 and out[15] == [], (out[14], out[15])
        # the account profile's cookie went back to its own site (the positive control: the
        # profile holds it) and never reached the public site, the file host or site C's page
        assert [c for c in sites.receipts.cookies if c[0].startswith(A) and "AcctSession42" in c[2]]
        assert not [c for c in sites.receipts.cookies if c[0].startswith((B, TRACK))]
        assert not [c for c in sites.receipts.cookies if c[0].startswith(C) and c[1] == "/home"]
    assert not _chrome_processes()


def test_off_site_navigation_moves_to_public_profile(tmp_path):
    """Design §4, S4-1: in an account run, a link off the site set is opened in the public
    profile, not refused, and no cookie of the account profile goes with it — although the
    account page's own request to that host carries the account's parent-domain cookie (the
    positive control: that profile would have sent it). Back on the site, the account profile
    still holds its cookie."""
    news = "news.site-a.example"           # off the site set, under the site's parent domain
    home = controller_home(tmp_path)
    with ChromeSites([A, news]) as sites:
        _controller_uid(sites)
        sites.pages["/home"] = ("text/html", (
            f'<title>Home</title><p>Account home</p><img src="https://{news}/pixel.png" alt="">'
            f'<a href="https://{news}/news">News</a>').encode(),
            {"Set-Cookie": "acct=AcctParent88; Domain=site-a.example; Secure; Path=/; SameSite=None; "
             "Max-Age=3600"})
        sites.pages["/pixel.png"] = ("image/png", b"\x89PNG\r\n\x1a\n", {})
        sites.pages["/news"] = ("text/html", b"<title>News</title><p>Public news</p>", {})
        steps = [
            site_step("soff", A),   # its own profile: the cookie set here stays out of other tests
            {"run": {"name": "r", "request_id": "it-offsite", "sites": [["soff", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"sleep": 1},
            {"observe": {"name": "r"}},                                      # 4
            {"observe": {"name": "r"}},                                      # 5 (the cookie is set)
            {"act": {"name": "r", "kind": "click", "label": "News"}},        # 6
            {"sleep": 1},
            {"observe": {"name": "r"}},                                      # 8
            {"act": {"name": "r", "kind": "navigate", "label": "Open site: soff"}},   # 9
            {"sleep": 1},
            {"observe": {"name": "r"}},                                      # 11
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=home, broker_fixture=fixture_config(tmp_path))
        cookies = sites.receipts.cookies
        hits_ = [(r[0], r[1].split(":")[0], r[2]) for r in sites.receipts.requests]
    assert "Account home" in out[4]["text"] and out[4]["source_id"] is None, out[4]
    assert out[6].get("outcome") == "done", out[6]          # free: a public link, not refused
    assert "Public news" in out[8]["text"] and out[8]["source_id"], out[8]   # a public page
    assert ("GET", news, "/news") in hits_, hits_
    # the positive control: the account page's own request to that host carried the cookie
    assert [c for c in cookies if c[0].startswith(news) and c[1] == "/pixel.png"
            and "AcctParent88" in c[2]], cookies
    # the public profile sent no cookie of the account profile
    assert not [c for c in cookies if c[0].startswith(news) and c[1] == "/news"], cookies
    # back on the site: the account profile kept its cookie
    assert "Account home" in out[11]["text"], out[11]
    assert len([c for c in cookies if c[0].startswith(A) and c[1] == "/home"
                and "AcctParent88" in c[2]]) >= 1, cookies
    assert not _chrome_processes()


# --------------------------------------------------------------------------- open web


def _profiles_listing() -> list[str]:
    r = subprocess.run(["find", "/data/profiles", "-printf", "%P %s %T@\n"], capture_output=True, text=True)
    return sorted(r.stdout.splitlines())


def test_open_web_blank_search_sources_and_profiles_untouched(tmp_path):
    """Two open-web runs and one account run. The first open-web run gets a cookie from the
    site and sends it back within the run (the positive control: cookies work); the second
    starts blank and sends none. Neither touches /data/profiles, which the account run then
    does (the positive control: the listing sees a profile change)."""
    home = controller_home(tmp_path)
    profiles_before = _profiles_listing()
    marks = []
    with ChromeSites([SEARCH, B]) as sites:
        _controller_uid(sites)
        sites.pages["/search"] = ("text/html", (
            b"<title>Results</title><p>Search results</p>"
            b'<a href="https://pub.site-b.example/article">An article</a>'), {})
        sites.pages["/article"] = ("text/html", (
            b"<title>The article</title><p>Article body</p><img src=\"/logo.png\" alt=\"\">"
            b'<a href="https://pub.site-b.example/paper.pdf">Paper PDF</a>'),
            {"Set-Cookie": "seen=OpenWebVisit7; Secure; Path=/"})
        sites.pages["/logo.png"] = ("image/png", b"\x89PNG\r\n\x1a\n", {})
        sites.pages["/paper.pdf"] = ("application/pdf", PDF, {})

        def journey(name, rid):
            return [
                {"run": {"name": name, "request_id": rid, "mode": "open_web"}},
                {"wait_state": {"name": name, "state": "running"}},
                {"observe": {"name": name}},
                {"search": {"name": name, "query": "synthetic article"}},
                {"act": {"name": name, "kind": "click", "label": "An article"}},
                {"sleep": 1},
                {"observe": {"name": name}},
            ]

        def snapshot(proc, line):
            marks.append((len(sites.receipts.cookies), _profiles_listing(), len(sites.receipts.requests)))

        steps = [
            *journey("r", "it-open"),                                         # 0–6
            {"download": {"name": "r", "label": "Paper PDF"}},                # 7
            {"sources": {"name": "r"}},                                       # 8
            {"surfaces": {"name": "r"}},                                      # 9
            {"finish": {"name": "r"}},                                        # 10
            {"ready": {"hold": 1}},                                           # 11
            *journey("r2", "it-open-2"),                                      # 12–18
            {"finish": {"name": "r2"}},                                       # 19
            {"ready": {"hold": 1}},                                           # 20
            site_step("sp", B, start="/article"),                             # 21
            {"run": {"name": "acct", "request_id": "it-open-control", "sites": [["sp", "account"]]}},
            {"wait_state": {"name": "acct", "state": "running"}},
            {"observe": {"name": "acct"}},
            {"finish": {"name": "acct"}},
        ]
        out, blob = scanned(steps, home=home, sites=sites, broker_fixture=fixture_config(tmp_path),
                            on_ready=snapshot)
        assert "No page is open yet" in out[2]["text"]
        assert "Search results" in out[3]["text"] and out[3]["source_id"]
        assert "Article body" in out[6]["text"] and out[6]["source_id"]
        assert out[7]["head"] == "%PDF-"
        urls = sorted(u for _, u, _ in out[8])
        assert urls == sorted([f"https://{B}/article",
                               f"https://{SEARCH}/search?q=synthetic+article"]), urls
        assert {sid for sid, _, _ in out[8]} == {out[3]["source_id"], out[6]["source_id"]}
        assert "Article body" in blob     # positive control: the replies are scanned
        assert "/article" not in blob and "q=synthetic" not in blob
        assert len(marks) == 2, marks
        first_run = sites.receipts.cookies[:marks[0][0]]
        second_run = sites.receipts.cookies[marks[0][0]:marks[1][0]]
        # cookies work: the first run's article set one and its image request carried it back
        assert [c for c in first_run if c[1] == "/logo.png" and "OpenWebVisit7" in c[2]], first_run
        # the second run started blank: the requests it made before the site set the cookie
        # again — the search and the article itself — carried none of the first run's
        assert not [c for c in second_run if c[1] in ("/search", "/article")], second_run
        second_requests = [r[2] for r in sites.receipts.requests[marks[0][2]:marks[1][2]]]
        assert any(p.startswith("/search") for p in second_requests) and "/article" in second_requests
        assert "Article body" in out[18]["text"]
        # neither open-web run touched the profiles; the account run did (the control)
        assert marks[0][1] == marks[1][1] == profiles_before
        assert out[25] == "completed", out[25]
    assert _profiles_listing() != profiles_before
    assert not _chrome_processes()


# --------------------------------------------------------------------------- logins


def _vault_snapshot(config) -> list:
    return json.loads((Path(config["root"]) / "fake-vault.json").read_text())


def test_login_with_password_and_totp_then_forget_keeps_the_vault_item(tmp_path):
    config = fixture_config(tmp_path)
    home = controller_home(tmp_path)
    item = _vault_snapshot(config)[0]
    password = next(f["value"] for f in item["fields"] if f["id"] == "password")
    username = next(f["value"] for f in item["fields"] if f["id"] == "username")
    with ChromeSites([LOGIN, A]) as sites:
        _controller_uid(sites)
        sites.pages["/username"] = form("username", "/password")
        sites.pages["/password"] = form("current-password", "/otp")
        sites.pages["/otp"] = form("one-time-code", "/finish")
        sites.pages["/finish"] = ("text/html", b"", {"Location": f"https://{A}/done"})
        sites.status["/finish"] = 303
        sites.pages["/done"] = ("text/html", b"<title>Home</title><p>Signed in</p>", {})
        steps = [
            {"site": {"site_id": "s1", "name": "Site A", "origins": [f"https://{A}"],
                      "login": [f"https://{LOGIN}"], "item": "item",
                      "start": f"https://{LOGIN}/username"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{LOGIN}"], "operator_ref": "op"}},
            {"broker": {"op": "status"}},   # was the guest confirmation (gone, slice 7)
            {"run": {"name": "r", "request_id": "it-totp", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 3.5},
            {"observe": {"name": "r"}},                                      # 6
            {"finish": {"name": "r"}},                                       # 7
            {"forget": {"site_id": "s1"}},                                   # 8
            {"surfaces": {"name": "r"}},                                     # 9
        ]
        out, blob = scanned(steps, home=home, sites=sites, broker_fixture=config)
        assert "Signed in" in out[6]["text"], out[6]
        posts = [r for r in sites.receipts.requests if r[0] == "POST"]
        assert [p[2] for p in posts] == ["/password", "/otp", "/finish"], posts
        bodies = {p[2]: p[3].decode() for p in posts}
        # each step got its own role's value: the username, the password, then a TOTP code
        assert bodies["/password"] == "value=" + username, bodies
        assert bodies["/otp"] == "value=" + password, bodies
        code = bodies["/finish"][len("value="):]
        assert bodies["/finish"].startswith("value=") and code and code not in (username, password)
        assert out[7] == "completed" and out[8] == {"site_id": "s1", "status": "forgotten"}, out[8]
        assert "Signed in" in blob        # positive control: the replies are scanned
        assert not scan(blob, password, username), "a credential reached a model or log surface"
        # forget never deletes the vault item
        assert [i["id"] for i in _vault_snapshot(config)] == ["item"]
        # positive control: forget did reach the broker — the site stays registered without its
        # item link, so its next login asks again (slice 5, Forget login = "ask me again")
        entry = json.loads((Path(config["root"]) / "registry.json").read_text())["s1"]
        assert entry["item_id"] is None and entry["login_origins"], entry
    assert not _chrome_processes()


def test_password_change_page_goes_to_the_live_view_and_provider_redirect_leaks_nothing(tmp_path):
    config = fixture_config(tmp_path)
    home = controller_home(tmp_path)
    item = _vault_snapshot(config)[0]
    password = next(f["value"] for f in item["fields"] if f["id"] == "password")
    change = ("text/html", (b'<form method=post action="/change"><input type=password '
                            b'autocomplete=current-password name=old><input type=password '
                            b'autocomplete=new-password name=new><button>Change</button></form>'), {})
    with ChromeSites([LOGIN, A, IDP]) as sites:
        _controller_uid(sites)
        sites.pages["/username"] = form("username", "/password")
        sites.pages["/password"] = change
        sites.pages["/oauth"] = ("text/html", b"", {
            "Location": f"https://{IDP}/authorize?client_id=casa-test&redirect_uri=https%3A%2F%2F{A}%2Fcallback"})
        sites.status["/oauth"] = 302
        steps = [
            {"site": {"site_id": "s1", "name": "Site A", "origins": [f"https://{A}"],
                      "login": [f"https://{LOGIN}"], "item": "item",
                      "start": f"https://{LOGIN}/username"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{LOGIN}"], "operator_ref": "op"}},
            {"broker": {"op": "status"}},   # was the guest confirmation (gone, slice 7)
            {"run": {"name": "r", "request_id": "it-change", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 3.5},
            {"events": {"name": "r"}},                                       # 6
            {"cancel": {"name": "r"}},
            {"surfaces": {"name": "r"}},                                     # 8
        ]
        out, blob = scanned(steps, home=home, sites=sites, broker_fixture=config)
        needed = [o for k, o in out[6] if k == "login_needed"]
        assert "password_change_page" in needed, out[6]
        posts = [r for r in sites.receipts.requests if r[0] == "POST"]
        # the username step was sent; the change page got nothing — no password was filled
        assert [p[2] for p in posts] == ["/password"], posts
        assert not [r for r in sites.receipts.requests if r[2].startswith("/change")]
        assert "login_needed" in blob     # positive control: the events are scanned
        assert not scan(blob, password)

        # a login whose step redirects to a provider the site never registered: the
        # credential never leaves for it, nothing reaches the provider
        sites.receipts.reset()
        sites.pages["/password"] = ("text/html", b"", {"Location": f"https://{LOGIN}/oauth"})
        sites.status["/password"] = 303
        steps2 = [
            # the redirect step is the site's own login page, so the browser does go on to the
            # provider's address — which the site never registered
            {"run": {"name": "r", "request_id": "it-idp", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 3.5},
            {"events": {"name": "r"}},
            {"cancel": {"name": "r"}},
        ]
        out2 = run_lifecycle(steps2, home=home, broker_fixture=config)
        assert IDP not in hosts_reached(sites), sites.receipts.requests
        # positive control: the login did reach the step that redirects to the provider (the
        # proxy's events for it carry no address: a login runs in the credential window)
        assert ("GET", LOGIN, "/oauth") in {(r[0], r[1].split(":")[0], r[2]) for r in sites.receipts.requests}
        assert not [r for r in sites.receipts.requests if password.encode() in r[3]]
        assert "login_needed" in [k for k, _ in out2[3]], out2[3]
    assert not _chrome_processes()


# --------------------------------------------------------------------------- dependency deaths


def test_broker_killed_during_a_login_resumes_nothing(tmp_path):
    """The broker dies while a login step's send is held at the site: the run is interrupted,
    the next step is never sent, and a durable notice says so."""
    import asyncio
    config = fixture_config(tmp_path)
    home = controller_home(tmp_path)
    with ChromeSites([LOGIN, A]) as sites:
        _controller_uid(sites)
        sites.pages["/username"] = form("username", "/password")
        sites.pages["/password"] = form("current-password", "/finish")
        sites.pages["/finish"] = ("text/html", b"", {"Location": f"https://{A}/done"})
        sites.status["/finish"] = 303
        sites.call(lambda: setattr(sites.harness.fixture, "hold_paths", {"/password"}))
        sites.call(lambda: setattr(sites.harness.fixture, "response_hold", asyncio.Event()))

        def kill_broker(proc, line):
            deadline = time.monotonic() + 60
            while not [r for r in sites.receipts.requests if r[0] == "POST"]:
                if time.monotonic() > deadline:
                    raise RuntimeError("the login step never reached the site")
                time.sleep(0.2)
            subprocess.run(["pkill", "-KILL", "-u", "casa-broker", "-f", "brokerfakes"], check=False)
            time.sleep(1)
            sites.call(lambda: sites.harness.fixture.response_hold.set())

        steps = [
            {"site": {"site_id": "s1", "name": "Site A", "origins": [f"https://{A}"],
                      "login": [f"https://{LOGIN}"], "item": "item",
                      "start": f"https://{LOGIN}/username"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{LOGIN}"], "operator_ref": "op"}},
            {"broker": {"op": "status"}},   # was the guest confirmation (gone, slice 7)
            {"run": {"name": "r", "request_id": "it-broker-kill", "sites": [["s1", "account"]]}},
            {"ready": {"hold": 0}},
            {"wait_state": {"name": "r", "state": "interrupted", "timeout": 120}},   # 5
            {"sleep": 3},
            {"events": {"name": "r"}},                                                # 7
            {"notices": None},                                                        # 8
            {"browser": None},                                                        # 9
        ]
        out = run_lifecycle(steps, home=home, broker_fixture=config, on_ready=kill_broker)
        assert out[5] == "interrupted"
        posts = [r[2] for r in sites.receipts.requests if r[0] == "POST"]
        assert posts == ["/password"], posts          # the held step only; nothing after it
        assert any(k == "run_interrupted" for k, _ in out[7]), out[7]
        assert out[9] is False
        # The broker is back (a fresh one over the same state): the interrupted run stays
        # interrupted and nothing of it is sent again, while a new run of the same login does
        # reach the next step (the positive control: a resumed login would have been seen).
        sites.call(lambda: setattr(sites.harness.fixture, "response_hold", None))
        sites.pages["/done"] = ("text/html", b"<title>Home</title><p>Signed in</p>", {})
        again = [
            {"states": None},                                                         # 0
            {"run": {"name": "r2", "request_id": "it-broker-kill-2", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r2", "state": "running", "timeout": 150}},
            {"sleep": 3.5},
        ]
        out2 = run_lifecycle(again, home=home, broker_fixture=config)
        assert list(out2[0].values()).count("interrupted") == 1, out2[0]
        posts = [r[2] for r in sites.receipts.requests if r[0] == "POST"]
        assert posts == ["/password", "/password", "/finish"], posts
    assert not _chrome_processes()


def test_controller_killed_during_a_download_leaves_no_file_and_no_run(tmp_path):
    """The controller dies while a download's response is held: at the next start the run is
    interrupted (a durable notice), no file record or partial remains, nothing is fetched again."""
    import asyncio
    import signal
    home = controller_home(tmp_path)
    with ChromeSites([SEARCH, B]) as sites:
        _controller_uid(sites)
        sites.pages["/docs"] = ("text/html", b"<title>Docs</title><p>Documents</p>"
                                b'<a href="https://pub.site-b.example/big.pdf">Big PDF</a>', {})
        sites.pages["/big.pdf"] = ("application/pdf", PDF, {})

        sites.pages["/search"] = ("text/html", b"<title>Results</title><p>Results</p>"
                                  b'<a href="https://pub.site-b.example/docs">Docs</a>', {})

        def kill(proc, line):
            deadline = time.monotonic() + 60
            while not [r for r in sites.receipts.requests if r[2] == "/big.pdf"]:
                if time.monotonic() > deadline:
                    raise RuntimeError("the download never reached the site")
                time.sleep(0.2)
            proc.send_signal(signal.SIGKILL)

        def hold(proc, line):
            sites.call(lambda: setattr(sites.harness.fixture, "response_hold", asyncio.Event()))

        steps = [
            {"run": {"name": "r", "request_id": "it-dl-kill", "mode": "open_web"}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"search": {"name": "r", "query": "docs"}},
            {"act": {"name": "r", "kind": "click", "label": "Docs"}},
            {"observe": {"name": "r"}},
            {"ready": {"hold": 2}},                     # the parent holds the site's responses
            {"download": {"name": "r", "label": "Big PDF", "background": True, "after": 0.5}},
            {"ready": {"hold": 600}},                   # the parent kills the controller here
        ]
        calls = []

        def on_ready(proc, line):
            calls.append(line)
            (hold if len(calls) == 1 else kill)(proc, line)

        assert run_lifecycle(steps, home=home, on_ready=on_ready, killed=True) is None
        sites.call(lambda: sites.harness.fixture.response_hold.set())
        sites.call(lambda: setattr(sites.harness.fixture, "response_hold", None))
        fetched = len([r for r in sites.receipts.requests if r[2] == "/big.pdf"])
        second = [{"states": None}, {"notices": None}, {"files": None}, {"browser": None},
                  {"sleep": 2}]
        out = run_lifecycle(second, home=home)
        assert set(out[0].values()) == {"interrupted"}, out[0]
        assert any(k == "run_interrupted" for k, _ in out[1]), out[1]
        assert out[2] == [] and out[3] is False, out
        assert len([r for r in sites.receipts.requests if r[2] == "/big.pdf"]) == fetched == 1
    assert not _chrome_processes()


# --------------------------------------------------------------------------- Chrome's own sends


def test_chromes_own_background_sends_are_never_a_question(tmp_path):
    """Chrome's own background traffic (its account checks, variations, GCM, updates…) in an
    account run is refused quietly (``browser_background``): never a question to the operator
    about Chrome telemetry (operator ruling 2026-10-22).
    Positive controls: Chrome did send such requests and the proxy refused them as that class,
    and a page's own send in the same run reached the site (design §4: the site's own traffic
    is free)."""
    from urllib.parse import urlsplit

    from casa_browser.infra import CHROME_BACKGROUND_HOSTS
    home = controller_home(tmp_path)
    with ChromeSites([A]) as sites:
        _controller_uid(sites)
        sites.pages["/home"] = ("text/html", b"<title>Home</title><p>Home</p>"
                                b"<button onclick=\"fetch('/own', {method: 'POST', body: 'k=v'})"
                                b".catch(() => 0)\">Send</button>", {})
        steps = [
            site_step("sa", A),
            {"run": {"name": "r", "request_id": "it-chrome-bg", "sites": [["sa", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "click", "label": "Send"}},
            {"sleep": 15},                  # Chrome's start-up background traffic
            {"questions": {"name": "r"}},     # 6
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=home, broker_fixture=fixture_config(tmp_path))
        background = [e for e in sites.events() if not e.allow and e.reason == "browser_background"]
        hosts = {urlsplit(e.origin).hostname for e in background if e.origin}
        assert background, "Chrome sent nothing in the background: the control did not run"
        assert A not in hosts and hosts & CHROME_BACKGROUND_HOSTS, hosts     # never the site's own
        assert out[6] == [], out[6]                                   # no question at all
        assert ("POST", A, "/own") in {(r[0], r[1].split(":")[0], r[2]) for r in sites.receipts.requests}
        assert not sites.receipts.requests or {r[1].split(":")[0] for r in sites.receipts.requests} <= {A}
    assert not _chrome_processes()


def test_a_plain_http_address_of_a_site_lands_on_https(tmp_path):
    """casa-test F16: Chrome sends an address without a scheme as plain http (its HTTPS
    upgrades are off under the driver); the site lock refused it. The proxy now answers a
    plain-http GET to a host a granted site registered over https with a redirect to that https
    address, never upstream: the page lands on https, and the redirect is no question to the
    operator. Positive control: the browser did ask over http (the redirect is in the events)."""
    home = controller_home(tmp_path)
    with ChromeSites([A]) as sites:
        _controller_uid(sites)
        sites.pages["/home"] = ("text/html", f'<title>Home</title><a href="http://{A}/next">Next</a>'.encode(), {})
        sites.pages["/next"] = ("text/html", b"<title>Next</title><p>Arrived over https</p>", {})
        steps = [
            site_step("sa", A),
            {"run": {"name": "r", "request_id": "it-http", "sites": [["sa", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "click", "label": "Next"}},
            {"sleep": 2},
            {"observe": {"name": "r"}},     # 6
            {"questions": {"name": "r"}},     # 7
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=home, broker_fixture=fixture_config(tmp_path))
        assert "Arrived over https" in out[6].get("text", ""), out[6]
        upgraded = [e for e in sites.events() if e.reason == "https_upgrade"]
        assert upgraded and all(not e.allow and e.origin == f"http://{A}" for e in upgraded), upgraded
        assert not any("http://" in t for _, t, _ in out[7]), out[7]          # no question about it
        assert ("GET", A, "/next") in {r[:3] for r in sites.receipts.requests}   # over https
    assert not _chrome_processes()
