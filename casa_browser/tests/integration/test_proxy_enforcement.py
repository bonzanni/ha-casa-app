"""Task 6 with the real browser, under design §4's request policy: requests branded Chrome
makes on its own — a service worker's sends, a real form's navigating POST, a prefetch, a
sibling origin sharing a parent domain's cookie — reach a fixture site when the account
session's page sends them, and a top-level document off the site never loads in the account
profile. Receipts are counted at the fixture's socket relay."""
from __future__ import annotations

import pytest

import proxyharness as h
from browserprobe import run_scenario
from casa_browser.protocol import SiteGrant
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker

APP, PUB = "app.sitea.example", "pub.sitea.example"


def _refused(sites, method, origin):
    """The attempts to ``origin`` that reached the proxy and were refused there (an event names
    the origin and method, never the path)."""
    return [e for e in sites.events()
            if not e.allow and e.method == method and e.origin == origin]


def _posts(sites, path):
    return [r for r in sites.receipts.requests if r[0] == "POST" and r[2].startswith(path)]


def _gets(sites, host, path):
    return [r for r in sites.receipts.requests if r[1].split(":")[0] == host and r[2].startswith(path)]


def test_service_worker_sends_are_the_sites_own_traffic():
    """Design §4: an account page's own sends — a service worker's included — go wherever
    the floor admits, on the site or off it."""
    with ChromeSites([APP, PUB]) as sites:
        sites.pages["/sw-page"] = ("text/html", b"""<html><body>sw<script>
            navigator.serviceWorker.register('/sw.js').then(() => document.title = 'registered');
        </script></body></html>""", {})
        sites.pages["/sw.js"] = ("text/javascript", b"""
            self.addEventListener('install', e => { self.skipWaiting(); });
            self.addEventListener('activate', e => { e.waitUntil(Promise.all([
                fetch('/sw-ping').catch(() => 0),
                fetch('/sw-post', {method: 'POST', body: 'k=v'}).catch(() => 0),
                fetch('https://%s/sw-off', {method: 'POST', mode: 'no-cors', body: 'o=1'}).catch(() => 0)])); });
        """ % PUB.encode(), {})
        s = h.site("sa", origins=(f"https://{APP}",), login=())
        sites.install(h.account_policy(sites=(s,)))
        run_scenario([{"open": {"site_id": "sa"}}, {"goto": f"https://{APP}/sw-page"}, {"wait": 6}])
        paths = [r[2] for r in sites.receipts.requests]
        assert "/sw.js" in paths and "/sw-ping" in paths  # the worker ran (positive control)
        assert len(_posts(sites, "/sw-post")) == 1
        assert [r[1].split(":")[0] for r in _posts(sites, "/sw-off")] == [PUB]
        assert not _refused(sites, "POST", f"https://{APP}")


def test_real_form_navigating_post_stays_on_the_site():
    """A real form's navigating POST to the site's own origin is sent; one whose action is
    another origin is a top-level document off the site: refused (``site_lock``), nothing
    reaches that origin."""
    with ChromeSites([APP, PUB]) as sites:
        sites.pages["/account"] = ("text/html", b"""<html><body><form method=post action=/account>
            <input name=a value=1><button id=go>Save</button></form></body></html>""", {})
        sites.pages["/away"] = ("text/html", b"""<html><body><form method=post action=https://%s/take>
            <input name=a value=1><button id=go>Send</button></form></body></html>""" % PUB.encode(), {})
        s = h.site("sa", origins=(f"https://{APP}",), login=())
        sites.install(h.account_policy(sites=(s,)))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": f"https://{APP}/account"},
                            {"click": "#go"}, {"wait": 2},
                            {"goto": f"https://{APP}/away"}, {"click": "#go"}, {"wait": 2}])
        assert res[1] == 200 and res[4] == 200
        assert len(_posts(sites, "/account")) == 1
        assert _posts(sites, "/take") == [] and not _gets(sites, PUB, "/")
        refused = _refused(sites, "POST", f"https://{PUB}")
        assert refused and {e.reason for e in refused} == {"site_lock"}


def test_prefetch_is_the_pages_own_traffic():
    """A prefetch is not a top-level document: on the site and off it, it is fetched."""
    with ChromeSites([APP, PUB]) as sites:
        sites.pages["/start"] = ("text/html", b"""<html><head>
            <link rel=prefetch href=/next><link rel=prefetch href=https://%s/other></head>
            <body>start</body></html>""" % PUB.encode(), {})
        s = h.site("sa", origins=(f"https://{APP}",), login=())
        sites.install(h.account_policy(sites=(s,)))
        run_scenario([{"open": {"site_id": "sa"}}, {"goto": f"https://{APP}/start"}, {"wait": 3}])
        assert _gets(sites, APP, "/next") and _gets(sites, PUB, "/other")


def test_sibling_public_origin_under_a_parent_domain_cookie():
    """The account profile holds a cookie for the parent domain. The site's own page may send
    to a sibling origin (its own traffic, design §4), but the account profile never loads a
    top-level document there: a navigation to the sibling is refused before any byte."""
    with ChromeSites([APP, PUB]) as sites:
        sites.pages["/login"] = ("text/html", b"<html><body>in</body></html>",
                                 {"Set-Cookie": "sid=parent; Domain=sitea.example; Secure; Path=/; Max-Age=3600"})
        sites.pages["/send"] = ("text/html", b"""<html><body><script>
            fetch('https://%s/collect', {method: 'POST', mode: 'no-cors', credentials: 'include', body: 'x=1'});
        </script></body></html>""" % PUB.encode(), {})
        app = h.site("sa", origins=(f"https://{APP}",), login=())
        pub = h.site("sp", origins=(f"https://{PUB}",), login=())
        policy = h.account_policy(sites=(app, pub),
                                  grants=(SiteGrant("sa", "account"), SiteGrant("sp", "public")))
        sites.install(policy)
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": f"https://{APP}/login"},
                            {"goto": f"https://{APP}/send"}, {"wait": 3},
                            {"goto": f"https://{PUB}/page"}, {"wait": 1}])
        assert res[1] == 200 and res[2] == 200
        assert len([r for r in sites.receipts.requests if r[1].split(":")[0] == PUB
                    and r[2] == "/collect"]) == 1
        assert not _gets(sites, PUB, "/page")
        refused = [e for e in sites.events() if not e.allow and e.origin == f"https://{PUB}"
                   and e.resource_type == "document"]
        assert refused and {e.reason for e in refused} == {"site_lock"}, refused
