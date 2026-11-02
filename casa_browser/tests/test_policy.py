from dataclasses import replace
import pytest
from casa_browser.protocol import Site, SiteGrant, ProviderBinding
from casa_browser.policy import RequestMeta, RunPolicy, SessionRef, Lease, decide

S = Site("s", "Account", ("https://account.example",), ("https://login.example",), None, (), None)
P = Site("p", "Public", ("https://public.example",), (), None, (), None)
BASE = RunPolicy(
    "run",
    "account",
    (SiteGrant("s", "account"), SiteGrant("p", "public")),
    (S, P),
    None,
    None,
    1,
    SessionRef("account", "s", 1),
)
SEND = RequestMeta("POST", "https://account.example/account", b"hello", resource_type="fetch")


PAGE_DOC = {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-fetch-site": "cross-site"}


def _doc(url):
    return RequestMeta("GET", url, resource_type="document", is_navigation=True)


PUBLIC = replace(BASE, active_session=SessionRef("public", None, 2))
OPEN_WEB = replace(BASE, mode="open_web", active_session=SessionRef("public", None, 1))


def test_account_document_off_site_is_site_lock():
    """Design §4: an account session's top-level documents stay on its own site's origins and
    login origins; any other origin — a public grant of the same run, another granted site,
    anywhere — is ``site_lock``, a form POST included."""
    for url in ("https://account.example/x?q=1", "https://login.example/sign-in"):
        d = decide(_doc(url), BASE)
        assert (d.allow, d.reason) == (True, "site_document"), url
    for url in ("https://public.example/", "https://outside.example/page"):
        d = decide(_doc(url), BASE)
        assert (d.allow, d.reason, d.event_kind) == (False, "site_lock", "request_refused"), url
        post = RequestMeta("POST", url, b"a=1", "application/x-www-form-urlencoded",
                           resource_type="document", is_navigation=True)
        assert decide(post, BASE).reason == "site_lock"
    other = replace(BASE, sites=(S, P, Site("t", "Other", ("https://other.example",), (), None,
                                             (), None)),
                    site_grants=(*BASE.site_grants, SiteGrant("t", "account")))
    assert decide(_doc("https://other.example/"), other).reason == "site_lock"


def test_account_subresource_any_floor_passing_origin_allowed():
    """Every other request of an account page — scripts, data, subframes, form-less sends —
    may go to any origin that passes the floor (reason ``site_page``), on the site or off it."""
    for url in ("https://account.example/api", "https://cdn.outside.example/app.js",
                "https://public.example/x?id=1"):
        for kind in ("script", "image", "fetch", "subframe", "stylesheet"):
            r = RequestMeta("POST" if kind == "fetch" else "GET", url, b"a=1" if kind == "fetch"
                            else b"", resource_type=kind)
            d = decide(r, BASE)
            assert (d.allow, d.reason) == (True, "site_page"), (url, kind)
    assert decide(replace(SEND, headers={"Next-Action": "mutate"}), BASE).reason == "site_page"


def test_account_websocket_and_preflight_are_free():
    ws = RequestMeta("GET", "wss://stream.outside.example/socket", resource_type="websocket")
    assert decide(ws, BASE).reason == "site_page"
    assert decide(replace(ws, url="wss://account.example/cdn-cgi/challenge-platform/x"),
                  BASE).reason == "site_page"
    pre = RequestMeta("OPTIONS", "https://api.outside.example/x", resource_type="other",
                      headers={"sec-fetch-dest": "empty"}, preflight_method="POST")
    assert decide(pre, BASE).reason == "site_page"


def test_unclassified_request_is_held_to_the_document_rule():
    """Plain http carries no ``Sec-Fetch-*``: a request whose class the browser did not state
    may be a top-level navigation, so it is held to the document rule."""
    from casa_browser.proxy_addon import request_meta
    bare = request_meta("GET", "http://outside.example/", b"", {})
    assert bare.resource_type == "other"
    assert decide(bare, BASE).reason == "site_lock"
    assert decide(request_meta("GET", "http://account.example/x", b"", {}), BASE).allow is False
    on_site = request_meta("GET", "https://account.example/x", b"", {})
    assert decide(on_site, BASE).reason == "site_document"
    stated = request_meta("GET", "https://outside.example/x", b"", {"sec-fetch-dest": "empty"})
    assert decide(stated, BASE).reason == "site_page"


def test_public_session_any_origin_passes_floor():
    """The public session, in an account run or an open-web run, admits any origin the floor
    admits (reason ``public_session``) — a registered account site's origin too: it holds no
    cookie of the account profile."""
    for policy in (PUBLIC, OPEN_WEB):
        for r in (_doc("https://outside.example/page?q=1"), _doc("https://account.example/"),
                  replace(SEND, url="https://public.example/search"),
                  RequestMeta("GET", "wss://x.outside.example/", resource_type="websocket")):
            d = decide(r, policy)
            assert (d.allow, d.reason) == (True, "public_session"), r.url
    assert decide(SEND, replace(BASE, mode="open_web")).reason == "session_mismatch"
    assert decide(_doc("https://x.example/"),
                  replace(PUBLIC, active_session=SessionRef("public", "s", 2))).reason \
        == "session_mismatch"


@pytest.mark.parametrize("policy", [BASE, PUBLIC, OPEN_WEB], ids=["account", "public", "open_web"])
def test_floor_and_infrastructure_still_refuse_in_both(policy):
    """The floor refuses in every session, a download hop included; Chrome's own background
    sends stay refused; nothing private, local or numeric is ever admitted."""
    for url in ("http://127.0.0.1/", "http://localhost/", "http://thing.internal/",
                "http://single/", "http://[::1]/", "https://0x7f000001/", "https://router.lan/"):
        for kind in ("document", "fetch", "subframe"):
            r = RequestMeta("GET", url, resource_type=kind, is_navigation=kind == "document")
            assert decide(r, policy).reason == "floor", (url, kind)
            assert decide(r, policy, hop=True).reason == "floor"
    pub = RequestMeta("GET", "https://ok.example/", resource_type="fetch",
                      resolved_addresses=("192.168.1.10",))
    assert decide(pub, policy).reason == "floor"
    own = RequestMeta("POST", "https://clientservices.googleapis.com/x", b"a=1",
                      browser_own=True)
    assert decide(own, policy).reason == "browser_background"
    assert decide(own, policy, hop=True).reason == "browser_background"


def test_download_hop_is_floor_infrastructure_and_background_only():
    """S4-5: the downloader's hops never meet the account document rule."""
    hop = _doc("https://cdn.outside.example/file.pdf")
    assert decide(hop, BASE).reason == "site_lock"
    d = decide(hop, BASE, hop=True)
    assert (d.allow, d.reason) == (True, "download_hop")
    assert decide(hop, PUBLIC, hop=True).allow
    assert decide(hop, None, hop=True).reason == "session_mismatch"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://thing.internal/",
        "http://single/",
        "http://[::1]/",
        "http://224.0.0.1/",
    ],
)
def test_floor(url):
    assert (
        decide(
            RequestMeta("GET", url),
            replace(BASE, mode="open_web", active_session=SessionRef("public", None, 1)),
        ).reason
        == "floor"
    )


def test_login_lease_on_the_login_origin():
    lease = Lease("l", "run", "s", 1, "login")
    r = replace(SEND, url="https://login.example/login")
    assert decide(r, replace(BASE, login_lease=lease)).reason == "login_lease"
    assert decide(r, replace(BASE, login_lease=replace(lease, generation=2))).reason == "site_page"


def test_provider_binding_is_exact_and_lease_bound():
    """An exact tagged provider request requires both its lease and the proxy tagger."""
    tagger = _binding_tagger(canary().encode())
    binding = ProviderBinding(
        'https://provider.example', path_tag=tagger('path', b'/authorize'),
        client_id_tag=tagger('client_id', b'client'),
        redirect_uri_tag=tagger('redirect_uri', b'https%3A%2F%2Faccount.example%2Fcallback'))
    p = replace(
        BASE,
        sites=(replace(S, provider_bindings=(binding,)), P),
        login_lease=Lease("l", "run", "s", 1, "login"),
    )
    r = RequestMeta(
        "GET",
        "https://provider.example/authorize?client_id=client&redirect_uri=https%3A%2F%2Faccount.example%2Fcallback",
        resource_type="document",
        is_navigation=True,
    )
    assert decide(r, p, tagger=tagger).allow
    for url in [
        r.url + "&client_id=client",
        r.url.replace("client_id=client", "client_id=other"),
        r.url.replace("%3A", "%3a"),
    ]:
        assert not decide(replace(r, url=url), p, tagger=tagger).allow
    assert not decide(r, replace(p, login_lease=None), tagger=tagger).allow

    assert not decide(r, p).allow


def test_infrastructure_bounded_and_floor_first():
    r = RequestMeta(
        "POST",
        "https://login.example/v1/client/sessions/sess_123/tokens",
        b"token=x&organization_id=1&force_origin=true",
        "application/x-www-form-urlencoded",
    )
    assert decide(r, BASE).reason == "infrastructure"
    # outside the rule's shape: an ordinary request of the site (its own traffic is free)
    assert decide(replace(r, body=b"token=x&delete=true"), BASE).reason == "site_document"
    assert decide(replace(r, url=r.url.replace("login.example", "other.example")),
                  BASE).reason == "site_lock"
    assert decide(replace(r, resolved_addresses=("127.0.0.1",)), BASE).reason == "floor"
    assert (
        decide(
            RequestMeta(
                "GET", "https://api.hcaptcha.com/checksiteconfig?host=account.example&sitekey=abc"
            ),
            BASE,
        ).reason
        == "infrastructure"
    )
    assert not decide(RequestMeta("POST", "https://api.hcaptcha.com/delete"), BASE).allow


def test_manual_lease_keeps_the_document_rule():
    p = replace(BASE, manual_lease=Lease("m", "run", "s", 1, "manual"))
    assert decide(SEND, p).reason == "manual_lease"
    assert decide(_doc("https://account.example/x"), p).reason == "manual_lease"
    assert decide(_doc("https://outside.example/"), p).reason == "site_lock"
    assert decide(replace(SEND, url="https://outside.example/"), p).reason == "site_page"


def test_missing_policy():
    assert decide(RequestMeta("GET", "http://localhost/"), None).reason == "session_mismatch"


# Authorization-shaped requests are checked before the registered login-origin shortcut.
import hashlib
import hmac
from brokerfakes import canary


def _binding_tagger(key):
    return lambda label, raw: hmac.new(key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()


@pytest.mark.parametrize('case', ['unbound', 'other_client', 'duplicate', 'encoded_client',
                                  'uppercase', 'encoded_redirect', 'bad_escape', 'bad_utf8',
                                  'encoded_duplicate', 'exact', 'manual', 'ordinary', 'no_tagger'])
def test_provider_among_login_origins_requires_binding(case):
    """Authorization requests cannot bypass exact tagged binding through origin membership."""
    tagger = _binding_tagger(canary().encode())
    callback = b'https%3A%2F%2Faccount.example%2Fcallback'
    binding = ProviderBinding('https://provider.example', path_tag=tagger('path', b'/authorize'),
                              client_id_tag=tagger('client_id', b'client'),
                              redirect_uri_tag=tagger('redirect_uri', callback))
    s = replace(S, login_origins=(*S.login_origins, 'https://provider.example'),
                provider_bindings=() if case == 'unbound' else (binding,))
    p = replace(BASE, sites=(s, P), login_lease=Lease('l', 'run', 's', 1, 'login'))
    query = 'client_id=client&redirect_uri=' + callback.decode()
    if case == 'other_client':
        query = query.replace('=client&', '=other&')
    elif case == 'duplicate':
        query += '&client_id=client'
    elif case == 'encoded_client':
        query = query.replace('client_id=', 'client%5Fid=')
    elif case == 'uppercase':
        query = query.replace('client_id=', 'CLIENT_ID=')
    elif case == 'encoded_redirect':
        query = query.replace('redirect_uri=', 'redirect%5furi=')
    elif case == 'bad_escape':
        query = 'bad%xx=value'
    elif case == 'bad_utf8':
        query = 'bad%FF=value'
    elif case == 'encoded_duplicate':
        query += '&client%5fid=client'
    elif case == 'ordinary':
        query = 'state=ordinary'
    elif case == 'manual':
        p = replace(p, login_lease=None, manual_lease=Lease('m', 'run', 's', 1, 'manual'))
    request = RequestMeta('GET', 'https://provider.example/authorize?' + query,
                          resource_type='document', is_navigation=True)
    result = decide(request, p, tagger=None if case == 'no_tagger' else tagger)
    if case in ('exact', 'manual', 'ordinary'):
        assert result.allow
    else:
        assert not result.allow and result.reason == 'provider_binding'


@pytest.mark.parametrize('query', ['CLIENT_ID=client', 'redirect_uri=callback'])
def test_single_authorization_key_requires_binding(query):
    """Either decoded key alone must stop the wholesale login-origin pass."""
    policy = replace(BASE, login_lease=Lease('l', 'run', 's', 1, 'login'))
    request = RequestMeta('GET', 'https://login.example/authorize?' + query,
                          resource_type='document', is_navigation=True)
    result = decide(request, policy)
    assert not result.allow and result.reason == 'provider_binding'


def test_provider_binding_cannot_replay_at_other_login_origin():
    """Matching component tags authorize only their recorded provider origin."""
    tagger = _binding_tagger(canary().encode())
    binding = ProviderBinding('https://provider.example', tagger('path', b'/authorize'),
                              tagger('client_id', b'client'), tagger('redirect_uri', b'callback'))
    policy = replace(BASE, sites=(replace(S, provider_bindings=(binding,)), P),
                     login_lease=Lease('l', 'run', 's', 1, 'login'))
    request = RequestMeta('GET', 'https://login.example/authorize?client_id=client&redirect_uri=callback',
                          resource_type='document', is_navigation=True)
    result = decide(request, policy, tagger=tagger)
    assert not result.allow and result.reason == 'provider_binding'


PAGE_FETCH = {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "cross-site",
              "content-type": "application/x-www-form-urlencoded"}
BROWSER_OWN = {"content-type": "application/x-www-form-urlencoded"}
# as Chrome sends its own background requests (measured in the image lane, 2026-10-22)
CHROME_OWN = {"sec-fetch-site": "none", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "empty",
              "content-type": "application/x-www-form-urlencoded"}


def _meta(method, url, headers, body=b"x=1"):
    """Through the proxy's own construction (``proxy_addon.request_meta`` → ``classify``)."""
    from casa_browser.proxy_addon import request_meta
    return request_meta(method, url, body, dict(headers))


@pytest.mark.parametrize("url", [
    "https://clientservices.googleapis.com/chrome-variations/seed?osname=linux",
    "https://android.clients.google.com/c2dm/register3",
    "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard",
    "https://passwordsleakcheck-pa.googleapis.com/v1/leaks:lookupSingle",
    "https://rr5---sn-uhvcpaxoa-5hne.gvt1.com/edgedl/chrome/dict/x.bdic",
])
def test_chrome_background_refused_quietly(url):
    """Chrome's own background sends (no ``Sec-Fetch-*`` header at all, not a navigation) to its
    fixed background hosts are refused as ``browser_background`` (operator rulings
    2026-10-22). The same host asked by a page's ``fetch`` (``Sec-Fetch-Dest:
    empty``, which ``classify`` also calls ``other``) or by a navigation is an ordinary request."""
    for headers in (BROWSER_OWN, CHROME_OWN):
        own = _meta("POST", url, headers)
        d = decide(own, BASE)
        assert not d.allow and d.reason == "browser_background", headers
    page_fetch = _meta("POST", url, PAGE_FETCH)
    assert page_fetch.resource_type == own.resource_type == "other"   # the class alone cannot tell
    assert decide(page_fetch, BASE).reason == "site_page"            # a page's own request
    for site in ("cross-site", "none"):      # a typed address is "none" too: a navigation
        nav = _meta("GET", url, {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
                                 "sec-fetch-site": site}, b"")
        assert decide(nav, BASE).reason == "site_lock"


def test_chrome_background_lookalikes_are_ordinary():
    from casa_browser.infra import chrome_background
    for url in ("https://accounts.google.com.attacker.example/x", "https://evil-accounts.google.com/x",
                "https://rr5---sn-x.gvt1.com.attacker.example/", "https://xrr5---sn-x.gvt1.com/"):
        assert not chrome_background(_meta("POST", url, BROWSER_OWN)), url
    assert chrome_background(_meta("POST", "https://accounts.google.com/x", BROWSER_OWN))
    # Chrome's extension update stays admitted (an infrastructure exception, checked first)
    upd = _meta("GET", "https://clients2.google.com/service/update2/crx?x=id%3Dabc", {}, b"")
    assert decide(upd, BASE).reason == "infrastructure"


def test_https_upgrade_of_a_typed_http_address():
    """casa-test F16: a schemeless address typed into Chrome goes out as plain http, which the
    site lock refuses. A bodiless http GET on port 80 to a host a granted site registered over
    https gets that https address (same path and query); anything else gets none."""
    from casa_browser.policy import https_upgrade
    typed = RequestMeta("GET", "http://login.example/sign-in?next=%2Fhome")   # no Sec-Fetch over http
    assert decide(typed, BASE).reason == "site_lock"                     # what F16 met
    assert https_upgrade(typed, BASE) == "https://login.example/sign-in?next=%2Fhome"
    assert https_upgrade(RequestMeta("GET", "http://account.example"), BASE) == "https://account.example/"
    assert https_upgrade(RequestMeta("HEAD", "http://public.example/x"), BASE) == "https://public.example/x"
    for refused in (RequestMeta("POST", "http://login.example/x", body=b"a=1"),   # a send
                    RequestMeta("GET", "http://login.example/x", body=b"a"),
                    RequestMeta("GET", "http://login.example:8080/x"),           # another port
                    RequestMeta("GET", "http://elsewhere.example/x"),            # not registered
                    RequestMeta("GET", "http://login.example.attacker.example/"),
                    RequestMeta("GET", "https://login.example/x")):              # already https
        assert https_upgrade(refused, BASE) is None, refused.url
    ungranted = replace(BASE, site_grants=(SiteGrant("p", "public"),))
    assert https_upgrade(typed, ungranted) is None                         # only a granted site
    assert https_upgrade(typed, None) is None


# the 1Password extension's own calls, as measured in the image lane (2026-10-23)
EXTENSION_POST = {"origin": "chrome-extension://aeblfdkhhhdcdjpifhhbdiojplfjncoa",
                  "sec-fetch-site": "none", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}
EXTENSION_GET = {"sec-fetch-site": "none", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}


@pytest.mark.parametrize("method, url, headers", [
    ("POST", "https://my.1password.com/api/pre-registration-features", EXTENSION_POST),
    ("GET", "https://watchtower.1password.com/api/v1/compromised-sites", EXTENSION_GET),
    ("POST", "https://b5x-sentry.1passwordservices.com/api/1/envelope/", EXTENSION_POST),
    ("GET", "https://dl.google.com/release2/chrome_component/x/y.crx3", CHROME_OWN),
])
def test_the_browsers_and_extensions_own_calls_off_the_site_set_are_quiet(method, url, headers):
    """casa-test F10: a request the browser or its extension makes on its own
    (``Sec-Fetch-Site: none``, not a navigation) to a host outside the run's sites is refused
    as ``browser_background`` — whatever the host, no list. The same host from a page, or typed
    as an address, is the ordinary refusal; the same signal toward the site's own origin is not
    touched (it is decided as that site's request)."""
    body = b"x=1" if method == "POST" else b""
    own = _meta(method, url, headers, body)
    d = decide(own, BASE)
    assert not d.allow and d.reason == "browser_background"
    assert decide(_meta(method, url, PAGE_FETCH, body), BASE).reason == "site_page"   # a page
    typed = _meta("GET", url, {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
                              "sec-fetch-site": "none"}, b"")
    assert decide(typed, BASE).reason == "site_lock"
    on_site = _meta(method, "https://account.example/api/x", headers, body)
    assert decide(on_site, BASE).reason != "browser_background"
    # no Sec-Fetch-* at all (plain http carries none — a page's too) off Chrome's own hosts:
    # an ordinary refusal, never the quiet one
    bare = _meta(method, url.replace("https://", "http://"), BROWSER_OWN, body)
    assert bare.browser_own and decide(bare, BASE).reason == "site_lock"


def test_provider_binding_document_during_lease():
    """The one off-site top-level document of an account session: the exact recorded provider
    request, during the run's login lease. The provider's other pages, or the same request
    without the lease, are ``site_lock``/``provider_binding``."""
    tagger = _binding_tagger(canary().encode())
    binding = ProviderBinding('https://provider.example', tagger('path', b'/authorize'),
                              tagger('client_id', b'client'), tagger('redirect_uri', b'cb'))
    p = replace(BASE, sites=(replace(S, provider_bindings=(binding,)), P))
    lease = Lease("l", "run", "s", 1, "login")
    exact = _doc("https://provider.example/authorize?client_id=client&redirect_uri=cb")
    assert decide(exact, replace(p, login_lease=lease), tagger=tagger).reason == "login_lease"
    assert decide(exact, p, tagger=tagger).reason == "site_lock"
    assert decide(_doc("https://provider.example/signin"), replace(p, login_lease=lease),
                  tagger=tagger).reason == "site_lock"


def test_background_quiet_unchanged():
    """Chrome's own background sends and the browser's or extension's own calls off the site
    set stay quiet refusals in the account session; in the public session Chrome's own stay
    refused too."""
    own = _meta("POST", "https://clientservices.googleapis.com/x", CHROME_OWN)
    ext = _meta("POST", "https://my.1password.com/api/x", EXTENSION_POST)
    for policy in (BASE, PUBLIC):
        assert decide(own, policy).reason == "browser_background"
    assert decide(ext, BASE).reason == "browser_background"


@pytest.mark.parametrize("url", [
    "https://user:pass@example.com/", "https://user@example.com/",
    "https://account.example\\@evil.example/", "https://acc%6Funt.example/",
    "https://account.example:/", "https://account.example:0/", "ftp://account.example/",
    "https://account.example/a b", "https://account.exa\x7fmple/", "https://bücher.example/",
    "https:///nohost", 42,
])
def test_address_refuses_what_the_floor_cannot_read(url):
    """``identity.address`` (the module's one survivor, slice 7) refuses any address it cannot
    read exactly; the floor then refuses the request."""
    from casa_browser.identity import IdentityError, address
    with pytest.raises(IdentityError):
        address(url)
    if isinstance(url, str):
        open_web = replace(BASE, mode="open_web", active_session=SessionRef("public", None, 1))
        assert decide(RequestMeta("GET", url), open_web).reason == "floor"


def test_address_gives_the_canonical_origin_and_parts():
    from casa_browser.identity import address
    assert address("wss://Account.Example:443/s?q=1#f") == (
        "https://account.example", "/s", "q=1", True, "f", True)
    assert address("http://[2001:db8::1]:8080/")[0] == "http://[2001:db8::1]:8080"
    assert address("https://account.example")[1:] == ("", "", False, "", False)
