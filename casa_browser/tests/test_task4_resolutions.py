"""Task 4: the behaviours on which the two independent implementations diverged, pinned
as settled."""
from __future__ import annotations

import hashlib
import hmac

from brokerfakes import canary

import pytest

from casa_browser.policy import Lease, RequestMeta, RunPolicy, SessionRef, decide
from casa_browser.protocol import ProviderBinding, Site, SiteGrant

_BINDING_KEY = canary().encode()


def _tagger(label, raw):
    return hmac.new(_BINDING_KEY, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()


A = Site("site_a", "A", ("https://a.example",), ("https://login.a.example",), "item",
         (ProviderBinding("https://accounts.google.com", path_tag=_tagger("path", b"/o/oauth2/auth"),
                          client_id_tag=_tagger("client_id", b"cid-1"),
                          redirect_uri_tag=_tagger("redirect_uri", b"https://login.a.example/cb")),), None, 1)
PUB = Site("site_p", "P", ("https://pub.example",), (), None, (), None, 1)
C = Site("site_c", "C", ("https://c.example",), (), None, (), None, 1)


def pol(session=("account", "site_a", 1), mode="account", login=None,
        grants=(("site_a", "account"), ("site_p", "public"))):
    return RunPolicy("run1", mode, tuple(SiteGrant(*g) for g in grants), (A, PUB, C),
                     login, None, 1, SessionRef(*session))


ACCT = pol()
PUBLIC = pol(session=("public", None, 2))
OPEN = pol(session=("public", None, 1), mode="open_web", grants=())
LOGIN = pol(login=Lease("l1", "run1", "site_a", 1, "login"))
PROVIDER = ("https://accounts.google.com/o/oauth2/auth?client_id=cid-1"
            "&redirect_uri=https://login.a.example/cb&state=s1&scope=email")


@pytest.mark.parametrize("request_meta,policy,expected", [
    # WebSockets: a wss endpoint resolves to its https origin; an account page's socket is one
    # of its own requests (design §4).
    (RequestMeta("GET", "wss://a.example/sock", resource_type="websocket"), ACCT,
     (True, "site_page")),
    (RequestMeta("GET", "wss://pub.example/s", resource_type="websocket"), PUBLIC,
     (True, "public_session")),
    # Infrastructure precedes the site lock (plan precedence): hCaptcha is not a task origin.
    (RequestMeta("POST", "https://api.hcaptcha.com/checksiteconfig?v=1a2b&host=a.example"
                 "&sitekey=abc-123&sc=1&swa=1&spst=0", resource_type="xhr"), ACCT,
     (True, "infrastructure")),
    # Cloudflare's sensor posts a payload and its scripts carry a query (measured).
    (RequestMeta("POST", "https://a.example/cdn-cgi/challenge-platform/h/b/jsd/r/0.5:1:abc",
                 b"v_abc=payload", "text/plain", resource_type="xhr"), ACCT, (True, "infrastructure")),
    (RequestMeta("GET", "https://a.example/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"
                 "?ray=8f2a", resource_type="script"), ACCT, (True, "infrastructure")),
    (RequestMeta("POST", "https://login.a.example/v1/client/sessions/sess_1/tokens"
                 "?__clerk_api_version=2025-04-10&_clerk_js_version=5.67.0",
                 b"organization_id=", "application/x-www-form-urlencoded", resource_type="fetch"),
     ACCT, (True, "infrastructure")),
    # ...and stays bounded: outside its shape a request is the page's own (``site_page``), never
    # the exception's.
    (RequestMeta("POST", "https://login.a.example/v1/client/sessions/sess_1/tokens?_method=DELETE",
                 b"", "application/x-www-form-urlencoded", resource_type="fetch"), ACCT,
     (True, "site_page")),
    (RequestMeta("POST", "https://api.hcaptcha.com/checksiteconfig?delete=1", resource_type="xhr"),
     ACCT, (True, "site_page")),
    (RequestMeta("POST", "https://c.example/cdn-cgi/challenge-platform/h/b/x", b"p", "text/plain",
                 resource_type="xhr"), ACCT, (True, "site_page")),
    (RequestMeta("POST", "https://a.example/cdn-cgi/challenge-platform/h/b/x", b"p", "text/plain",
                 headers={"X-HTTP-Method-Override": "DELETE"}, resource_type="xhr"), ACCT,
     (True, "site_page")),
    # The exact provider binding passes under a login lease, outside the site set.
    (RequestMeta("GET", PROVIDER, resource_type="document", is_navigation=True), LOGIN,
     (True, "login_lease")),
    # Anything but the exact binding under a login lease is an ordinary request to an
    # origin outside the site set.
    (RequestMeta("GET", PROVIDER, resource_type="document", is_navigation=True), ACCT,
     (False, "site_lock")),
    (RequestMeta("GET", PROVIDER + "&client_id=cid-2", resource_type="document",
                 is_navigation=True), LOGIN, (False, "provider_binding")),
    # The public-address floor: browser-reinterpreted numeric hosts, multicast; global IPv6 passes.
    (RequestMeta("GET", "http://127.1/"), OPEN, (False, "floor")),
    (RequestMeta("GET", "http://224.0.0.1/"), OPEN, (False, "floor")),
    (RequestMeta("GET", "http://[2001:4860:4860::8888]/"), OPEN, (True, "public_session")),
    # The public session loads any origin, an account grant's too: it holds no account cookie.
    (RequestMeta("GET", "https://a.example/", resource_type="document", is_navigation=True),
     PUBLIC, (True, "public_session")),
    # An account session only exists for an account grant.
    (RequestMeta("GET", "https://pub.example/x.js", resource_type="script"),
     pol(session=("account", "site_p", 1)), (False, "session_mismatch")),
    # HEAD of a static asset is a page's request; a document class without the flag is a
    # top-level document, held to the site.
    (RequestMeta("HEAD", "https://a.example/img.png", resource_type="image"), ACCT,
     (True, "site_page")),
    (RequestMeta("GET", "https://a.example/account", resource_type="document"), ACCT,
     (True, "site_document")),
    (RequestMeta("GET", "https://c.example/account", resource_type="document"), ACCT,
     (False, "site_lock")),
])
def test_divergence_resolved(request_meta, policy, expected):
    d = decide(request_meta, policy, **({"tagger": _tagger} if request_meta.url.startswith(
        "https://accounts.google.com/") else {}))
    assert (d.allow, d.reason) == expected


@pytest.mark.parametrize("url", ["http://127.0x0.0.1/", "http://192.0xa8.1.1/", "http://0x7f.1/",
                                 "http://017700000001/", "http://1.2.3.0x4/", "http://x.123/",
                                 "http://a.0x/"])
def test_floor_refuses_every_numeric_host_spelling(url):
    """A browser reads any host ending in a number as IPv4 (decimal, hex, octal labels);
    only the canonical dotted quad is accepted, and only when global (review r3-c1-r1)."""
    assert decide(RequestMeta("GET", url), OPEN).reason == "floor"


@pytest.mark.parametrize("path", ["/cdn-cgi/challenge-platform/../../delete",
                                  "/cdn-cgi/challenge-platform/h/./x",
                                  "/cdn-cgi/challenge-platform/.."])
def test_infrastructure_refuses_dot_segments(path):
    """A server may normalise dot segments out of the rule's path (review r3-c1-r1)."""
    d = decide(RequestMeta("POST", "https://a.example" + path, b"confirm=yes",
                           "application/x-www-form-urlencoded", resource_type="xhr"), ACCT)
    assert d.reason != "infrastructure"


def test_registered_default_port_is_canonical():
    """A registered ``https://host:443`` origin matches requests to ``https://host``
    (review r3-c1-r1)."""
    from casa_browser.protocol import parse_site_registration
    reg = parse_site_registration({"display_name": "X", "origins": ["https://a.example:443"],
                                   "login_origins": ["https://login.a.example:443"],
                                   "operator_ref": "tg-1"})
    assert reg["origins"] == ("https://a.example",)
    assert reg["login_origins"] == ("https://login.a.example",)


def test_provider_binding_refuses_selector_headers():
    """The binding is an exact GET document load; a method-override header makes it
    something else (review r3-c1-r2)."""
    meta = RequestMeta("GET", PROVIDER, headers={"X-HTTP-Method-Override": "DELETE"},
                       resource_type="document", is_navigation=True)
    assert not decide(meta, LOGIN, tagger=_tagger).allow


# --- The general check (operator ruling 2026-10-20): before every fixed exception the request
# must carry nothing beyond that exception's declared shape; anything extra is not admitted by
# the exception, and is decided as an ordinary request of the session (design §4).

CLERK = RequestMeta("POST", "https://login.a.example/v1/client/sessions/sess_1/tokens",
                    b"organization_id=", "application/x-www-form-urlencoded", resource_type="fetch")
CF = RequestMeta("POST", "https://a.example/cdn-cgi/challenge-platform/h/b/jsd/r/abc",
                 b"payload", "text/plain", resource_type="xhr")
HCAP = RequestMeta("POST", "https://api.hcaptcha.com/checksiteconfig?v=1&host=a.example",
                   resource_type="xhr")
PROV = RequestMeta("GET", PROVIDER, resource_type="document", is_navigation=True)


@pytest.mark.parametrize("name,base,policy,reason", [
    ("clerk", CLERK, ACCT, "infrastructure"),
    ("cloudflare", CF, ACCT, "infrastructure"),
    ("hcaptcha", HCAP, ACCT, "infrastructure"),
    ("provider", PROV, LOGIN, "login_lease"),
])
def test_every_fixed_exception_requires_its_exact_shape(name, base, policy, reason):
    from dataclasses import replace

    kwargs = {"tagger": _tagger} if name == "provider" else {}
    assert decide(base, policy, **kwargs).reason == reason  # the exact shape passes
    extras = [
        replace(base, headers={"X-HTTP-Method-Override": "DELETE"}),
        replace(base, headers={"Next-Action": "abc"}),
        replace(base, headers={"Content-Encoding": "gzip"}),
        replace(base, method="PUT"),
        replace(base, url=base.url.split("?")[0].rstrip("/") + "/./x"),
        replace(base, url=base.url.split("?")[0].rstrip("/") + "/../x"),
    ]
    if name in ("hcaptcha", "provider"):  # these declare no body
        extras.append(replace(base, body=b"transfer=1", content_type="application/x-www-form-urlencoded"))
    if name == "provider":
        extras.append(replace(base, resource_type="xhr", is_navigation=False))
    for extra in extras:
        d = decide(extra, policy, **kwargs)
        assert d.reason != reason, (name, extra)
        if name == "provider":   # off the site: the document rule or the binding refuses
            assert not d.allow, extra


def test_exception_body_bounds_hold_without_a_content_type():
    """The body bound is its own part of the shape, not a side effect of the header check."""
    from dataclasses import replace

    for base, policy, reason in ((HCAP, ACCT, "infrastructure"), (PROV, LOGIN, "login_lease")):
        assert decide(replace(base, body=b"x"), policy,
                      **({"tagger": _tagger} if base is PROV else {})).reason != reason
    assert decide(replace(CF, body=b"x" * (262144 + 1)), ACCT).reason != "infrastructure"
    assert decide(replace(CF, body=b"x" * 262144), ACCT).reason == "infrastructure"
