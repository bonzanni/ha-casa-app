"""Fixed infrastructure exceptions: site mechanics that always pass (design §5.3, v8).

Each rule is method + exact host (or a host bound to the task) + a bounded path, query and
body shape — never a whole-vendor bypass. They apply only while an account session is
active (a public session already has open-web treatment). The public-address floor and the
address check run before them (``policy.decide``).

* ``clerk_refresh`` — an auth vendor's session refresh (measured on RunPod:
  ``POST clerk.<site>/v1/client/sessions/{id}/tokens[/{template}]``, form fields
  ``token, organization_id, force_origin``); the host must be one of the ACTIVE account
  site's registered login origins.
* ``cloudflare_challenge`` — Cloudflare's ``/cdn-cgi/challenge-platform/`` sensor on an
  origin of the task's site set. Its scripts carry query data and its sensor POSTs a
  payload (measured: the ChatGPT run needed it), so the query is bounded in length and
  alphabet rather than by key, and the body is bounded in size.
* ``hcaptcha_checksiteconfig`` — ``api.hcaptcha.com/checksiteconfig`` (Stripe's hCaptcha).

**One general check (operator ruling, 2026-10-20).** Before any fixed exception — these rules,
the provider binding and a CORS preflight (``policy.decide``) — the request must carry nothing
beyond that exception's declared ``Shape``: its methods, its body bound, the kept semantic
headers it allows (none but ``content-type``, and only where a body is declared), its resource
classes, and no ``.``/``..`` path segment. A request with anything extra is not the exception;
it is an ordinary request, decided by the session's rules like any other.

``ONEPASSWORD_REGION_HOSTS`` lists the fixed browser sign-in and extension hosts.
The configured account host joins its region at runtime; exact names only, never wildcards.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from casa_browser.identity import SEMANTIC_HEADERS, address

_QUERY_VALUE = re.compile(r"[A-Za-z0-9._~-]{0,64}")
_FREE_QUERY = re.compile(r"[A-Za-z0-9._~=&%-]{0,1024}")


@dataclass(frozen=True)
class Shape:
    """What a fixed exception admits beyond its own path/query rule."""

    methods: frozenset[str]
    max_body: int = 0
    headers: frozenset[str] = frozenset()  # kept semantic headers allowed (lowercase)
    resource_types: frozenset[str] | None = None  # None: any resource class


def within_shape(request, shape: Shape) -> bool:
    """The one general check: the request carries nothing beyond ``shape``."""
    if request.method not in shape.methods or len(request.body) > shape.max_body:
        return False
    kept = {k.lower() for k in request.headers if k.lower() in SEMANTIC_HEADERS}
    if request.content_type is not None:
        kept.add("content-type")
    if not kept <= shape.headers:
        return False
    if shape.resource_types is not None and request.resource_type not in shape.resource_types:
        return False
    path = urlsplit(request.url).path
    return not any(segment in (".", "..") for segment in path.split("/"))


@dataclass(frozen=True)
class InfraRule:
    name: str
    methods: frozenset[str]
    host: str | None
    path_pattern: re.Pattern
    # Allowed query keys (each at most once, value matching _QUERY_VALUE); ``None`` means
    # any query matching _FREE_QUERY.
    query_keys: frozenset[str] | None
    scope: str  # 'site_login_origin' | 'task_origin' | 'fixed_host'
    max_body: int = 0
    body_form_keys: frozenset[str] | None = None  # set: urlencoded form with these keys only
    # A rule's own query grammar (full match); when set it replaces ``query_keys``.
    query_pattern: re.Pattern | None = None

    @property
    def shape(self) -> Shape:
        return Shape(self.methods, self.max_body,
                     frozenset({"content-type"}) if self.max_body else frozenset())


INFRA_RULES = (
    InfraRule(
        "clerk_refresh",
        frozenset({"POST"}),
        None,
        re.compile(r"/v1/client/sessions/[A-Za-z0-9_-]{1,128}/tokens(?:/[A-Za-z0-9_-]{1,128})?"),
        frozenset({"_clerk_js_version", "__clerk_api_version"}),
        "site_login_origin",
        max_body=16384,
        body_form_keys=frozenset({"token", "organization_id", "force_origin"}),
    ),
    InfraRule(
        "cloudflare_challenge",
        frozenset({"GET", "POST"}),
        None,
        re.compile(r"/cdn-cgi/challenge-platform/[A-Za-z0-9_.:/-]{1,512}"),
        None,
        "task_origin",
        max_body=262144,
    ),
    InfraRule(
        "hcaptcha_checksiteconfig",
        frozenset({"GET", "POST"}),
        "api.hcaptcha.com",
        re.compile(r"/checksiteconfig"),
        frozenset({"v", "host", "sitekey", "sc", "swa", "spst"}),
        "fixed_host",
    ),
    # Chrome's own extension and component updates (Plan 1 Task 8, measured): the managed
    # policy force-installs the 1Password extension through these exact hosts; nothing else
    # of Google's is admitted.
    InfraRule(
        "chrome_extension_update",
        frozenset({"GET"}),
        "clients2.google.com",
        re.compile(r"/service/update2/crx"),
        None,
        "fixed_host",
        query_pattern=re.compile(r"[A-Za-z0-9._~=&%,-]{1,2048}"),
    ),
    InfraRule(
        "chrome_extension_download",
        frozenset({"GET"}),
        "clients2.googleusercontent.com",
        re.compile(r"/crx/blobs/[A-Za-z0-9_.=/-]{1,1024}"),
        None,
        "fixed_host",
    ),
    InfraRule(
        "chrome_update_check",
        frozenset({"POST"}),
        "update.googleapis.com",
        re.compile(r"/service/update2/json"),
        None,
        "fixed_host",
        max_body=262144,
        # Chrome's request signature (CUP-ECDSA): a key id and version, and the request hash.
        query_pattern=re.compile(r"cup2key=[0-9]{1,4}:[A-Za-z0-9_-]{1,128}&cup2hreq=[0-9a-f]{64}"),
    ),
)

# Chrome's own background traffic (measured, Plan 1 S0 b/d/e and the image lane): the
# variations seed, GCM, the browser's account checks, component/extension updates and their
# download caches, the password leak check, Safe Browsing's relay, optimisation hints, static
# start-up resources — and the 1Password extension's own calls (casa-test F10, measured in the
# image lane: ``my.1password.com`` with ``Origin: chrome-extension://…``, ``watchtower.
# 1password.com`` without one). In an account session a request the browser or its extension
# makes on its own and not a navigation is refused quietly (``browser_background``): never a
# candidate, never a question to the operator. "On its own" is the browser's signal where it
# can be read (``browser_initiated``): ``Sec-Fetch-Site: none`` outside a navigation (Chrome
# sends it on its own requests and an extension's; a page cannot — a page's ``fetch`` carries
# ``Sec-Fetch-Dest: empty`` but a real ``Sec-Fetch-Site``), to any host outside the run's site
# set. A request with no ``Sec-Fetch-*`` header at all (plain http carries none, a page's
# included) counts only to the hosts below (``chrome_background``). The exceptions above that
# Chrome needs (extension and component updates) and the 1Password account's admitted hosts
# are admitted before either check. Exact names, plus Google's per-cache download hosts by
# their fixed shape.
CHROME_BACKGROUND_HOSTS: frozenset[str] = frozenset({
    "accounts.google.com", "android.clients.google.com", "clients2.google.com",
    "clients2.googleusercontent.com", "clientservices.googleapis.com",
    "content-autofill.googleapis.com", "ohttp-relay-safebrowsing-chrome.google.fastly-edge.com",
    "optimizationguide-pa.googleapis.com", "passwordsleakcheck-pa.googleapis.com",
    "redirector.gvt1.com", "safebrowsing.googleapis.com", "update.googleapis.com",
    "www.gstatic.com",   # measured in the image lane: a GET Chrome makes on its own at start
})
_GVT1_CACHE = re.compile(r"r{1,2}[0-9]{1,3}---sn-[a-z0-9-]{1,64}\.gvt1\.com")


def chrome_background(request) -> bool:
    """A request Chrome makes on its own to one of its background hosts (see above)."""
    if request.is_navigation or not request.browser_own:
        return False
    host = (urlsplit(request.url).hostname or "").lower()
    return host in CHROME_BACKGROUND_HOSTS or bool(_GVT1_CACHE.fullmatch(host))


def browser_initiated(request) -> bool:
    """A request the browser or its extension makes on its own, by the browser's own signal:
    ``Sec-Fetch-Site: none`` outside a navigation (see above). ``policy.decide`` asks it only
    for a request outside the run's site set, so it never changes what a site's own request
    is."""
    return (not request.is_navigation and request.browser_own
            and (request.headers or {}).get("sec-fetch-site", "").lower() == "none")


ONEPASSWORD_EXTENSION_HOSTS: frozenset[str] = frozenset()
BROKER_SDK_HOSTS: frozenset[str] = frozenset()
# 1Password's per-region service hosts the browser extension needs to sign in, unlock and sync
# (measured, Plan 1 S0 e, on an EU account: sign-in pages, account cookie, static app, user
# content, and the push WebSocket). They sit under 1Password's documented region domains and
# name no account; the account's own host comes from the options at runtime.
ONEPASSWORD_REGION_HOSTS = {
    region: frozenset({f"my.1password.{region}", f"accounts.1password.{region}",
                       f"app.1password.{region}", f"b5n.1password.{region}",
                       f"a.1passwordusercontent.{region}"})
    for region in ("eu", "com", "ca")
}


def onepassword_extension_hosts(account_host: str | None) -> frozenset[str]:
    """The exact hosts admitted for browser sign-in and extension traffic: the account host and its region's hosts."""
    if not account_host:
        return frozenset()
    region = account_host.rsplit(".", 1)[-1]
    return frozenset({account_host}) | ONEPASSWORD_REGION_HOSTS.get(region, frozenset())


def _pairs(text):
    """``[(key, value)]`` of an ``&``-separated string, or ``None`` when a part has no
    ``=`` or a key repeats."""
    if not text:
        return []
    parts = text.split("&")
    if any("=" not in p for p in parts):
        return None
    pairs = [tuple(p.split("=", 1)) for p in parts]
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        return None
    return pairs


def _query_ok(rule, query):
    if rule.query_pattern is not None:
        return rule.query_pattern.fullmatch(query) is not None
    if rule.query_keys is None:
        return _FREE_QUERY.fullmatch(query) is not None
    pairs = _pairs(query)
    if pairs is None:
        return False
    return all(k in rule.query_keys and _QUERY_VALUE.fullmatch(v) for k, v in pairs)


def _form_ok(rule, request):
    if rule.body_form_keys is None or not request.body:
        return True
    ct = request.content_type or next(
        (v for k, v in request.headers.items() if k.lower() == "content-type"), "")
    if ct.split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        return False
    pairs = _pairs(request.body.decode("ascii"))
    return pairs is not None and all(k in rule.body_form_keys for k, _ in pairs)


def infra_match(request, policy, extra_hosts: frozenset[str] = frozenset()) -> str | None:
    try:
        if policy is None or policy.mode != "account" or policy.active_session.kind != "account":
            return None
        active = next(
            (s for s in policy.sites
             if s.site_id == policy.active_session.site_id
             and any(g.site_id == s.site_id and g.access == "account" for g in policy.site_grants)),
            None)
        if active is None:
            return None
        origin, path, query, _, _, _ = address(request.url)
        u = urlsplit(request.url)
        if not origin.startswith("https://") or u.port not in (None, 443):
            return None
        if u.hostname in ONEPASSWORD_EXTENSION_HOSTS or u.hostname in extra_hosts:
            return "onepassword_extension"
        if u.scheme != "https" or request.resource_type == "websocket":
            return None
        task_origins = {
            o for s in policy.sites
            if any(g.site_id == s.site_id for g in policy.site_grants)
            for o in (*s.origins, *s.login_origins)
        }
        for rule in INFRA_RULES:
            if request.method not in rule.methods or not rule.path_pattern.fullmatch(path):
                continue
            if rule.scope == "fixed_host" and u.hostname != rule.host:
                continue
            if rule.scope == "site_login_origin" and origin not in active.login_origins:
                continue
            if rule.scope == "task_origin" and origin not in task_origins:
                continue
            if not within_shape(request, rule.shape):
                continue
            if not _query_ok(rule, query) or not _form_ok(rule, request):
                continue
            return rule.name
    except (ValueError, UnicodeError, AttributeError):
        pass
    return None
