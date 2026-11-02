"""Pure enforcing-proxy policy. Every refusal is a quiet typed event."""

from dataclasses import dataclass, field
from collections.abc import Mapping
import ipaddress
import re
from urllib.parse import urlsplit, unquote_plus
from casa_browser.protocol import Site, SiteGrant
from casa_browser.identity import IdentityError, address
from casa_browser.infra import Shape, browser_initiated, chrome_background, infra_match, within_shape


@dataclass(frozen=True)
class SessionRef:
    kind: str
    site_id: str | None
    generation: int


@dataclass(frozen=True)
class Lease:
    lease_id: str
    run_id: str
    site_id: str
    generation: int
    kind: str


@dataclass(frozen=True)
class RunPolicy:
    run_id: str
    mode: str
    site_grants: tuple[SiteGrant, ...]
    sites: tuple[Site, ...]
    login_lease: Lease | None
    manual_lease: Lease | None   # a live view's lease (login or assist)
    revision: int
    active_session: SessionRef


@dataclass(frozen=True)
class RequestMeta:
    method: str
    url: str = field(repr=False)
    body: bytes = field(default=b"", repr=False)
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    resource_type: str = "other"
    is_navigation: bool = False
    preflight_method: str | None = None
    resolved_addresses: tuple[str, ...] = ()
    # The browser made it on its own (``proxy_addon._browser_own``: no ``Sec-Fetch-*`` header,
    # or ``Sec-Fetch-Site: none`` outside a navigation — no page can send either). Set only by
    # the proxy from the real headers; every other caller leaves it False.
    browser_own: bool = False


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    event_kind: str


# The fixed provider exception besides infra.INFRA_RULES, as a declared shape
# (infra.within_shape).
PROVIDER_SHAPE = Shape(frozenset({"GET"}), resource_types=frozenset({"document"}))


def _yes(reason):
    return Decision(True, reason, "request_allowed")


def _no(reason, event="send_blocked"):
    return Decision(False, reason, event)


def _global(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.is_global and not ip.is_multicast and not ip.is_unspecified and not ip.is_reserved
    except ValueError:
        return False


def _ends_in_number(host):
    last = host.split(".")[-1]
    return bool(last) and (last.isdigit() or (last[:2] == "0x" and all(
        c in "0123456789abcdef" for c in last[2:])))


def _floor(request):
    try:
        address(request.url)
        host = urlsplit(request.url).hostname.lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (
                "." not in host
                or host == "localhost"
                or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa"))
            ):
                return False
            # A host whose last label is a number is an IPv4 address to a browser (the
            # WHATWG URL parser reads decimal, 0x-hex and 0-octal labels, in any count).
            # Only the canonical dotted quad is accepted, and it parsed above.
            if _ends_in_number(host):
                return False
        else:
            if not _global(host):
                return False
        return all(_global(ip) for ip in request.resolved_addresses)
    except (ValueError, AttributeError):
        return False


def _lease(lease, kind, policy):
    return (
        lease is not None
        and lease.kind == kind
        and lease.run_id == policy.run_id
        and lease.site_id == policy.active_session.site_id
        and lease.generation == policy.active_session.generation
    )


def _authorization_query(request):
    parts = urlsplit(request.url)
    pairs = [member.partition('=') for member in parts.query.split('&')]
    try:
        if any(re.search(r'%(?![0-9a-fA-F]{2})', name) for name, _, _ in pairs):
            raise ValueError
        names = [unquote_plus(name, errors='strict').lower() for name, _, _ in pairs]
        shaped = any(name in ('client_id', 'redirect_uri') for name in names)
    except (ValueError, UnicodeError):
        names, shaped = [], True
    return parts, pairs, names, shaped


def https_upgrade(request: RequestMeta, policy: RunPolicy | None) -> str | None:
    """casa-test F16: a typed address without a scheme goes out as plain http (Chrome's own
    HTTPS upgrades are off under the driver). A bodiless plain-http GET on port 80 to a host
    whose ``https://`` origin a granted site registered is answered by the proxy itself with a
    redirect to that https address (the same path and query) — never sent upstream; the https
    request is then decided like any other. ``None`` for anything else."""
    if policy is None or request.method not in ("GET", "HEAD") or request.body:
        return None
    try:
        parts = urlsplit(request.url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "http" or parts.port not in (None, 80) or not host:
            return None
    except ValueError:
        return None
    granted = {g.site_id for g in policy.site_grants}
    target = "https://" + host
    if not any(target in (*s.origins, *s.login_origins)
               for s in policy.sites if s.site_id in granted):
        return None
    return target + (parts.path or "/") + ("?" + parts.query if parts.query else "")


def _unclassified(request: RequestMeta) -> bool:
    """A request whose class the browser did not state: no ``Sec-Fetch-Dest`` (plain http
    carries no ``Sec-Fetch-*`` at all, a top-level navigation included). Held to the document
    rule, since it may be one."""
    return request.resource_type == "other" and not any(
        k.lower() == "sec-fetch-dest" for k in (request.headers or {}))


def decide(request: RequestMeta, policy: RunPolicy | None,
           extra_hosts: frozenset[str] = frozenset(), *, tagger=None,
           hop: bool = False) -> Decision:
    """``extra_hosts`` joins ``infra.ONEPASSWORD_EXTENSION_HOSTS`` at runtime: the 1Password
    account's own host, which comes from the app options and is never a literal here.

    Design §4 (action model, slice 4). Every session: the floor, the infrastructure rules and
    Chrome's background refusals. A public session (any run mode) then admits any origin. An
    account session's top-level documents stay on its site's origins and login origins (or
    the provider binding during a login lease); every other request of its pages is free.
    ``hop`` is the downloader's listener: floor, infrastructure and background only, never the
    document rule."""
    if policy is None:
        return _no("session_mismatch", "request_refused")
    if not _floor(request):
        return _no("floor", "request_refused")
    try:
        origin = address(request.url)[0]
    except IdentityError:
        return _no("unidentifiable", "request_refused")
    session = policy.active_session
    if session.kind not in ("public", "account") or (
        session.kind == "public" and session.site_id is not None
    ):
        return _no("session_mismatch", "request_refused")
    if policy.mode not in ("open_web", "account") or (
            policy.mode == "open_web" and session.kind != "public"):
        return _no("session_mismatch", "request_refused")
    sites = [s for s in policy.sites if any(g.site_id == s.site_id for g in policy.site_grants)]
    active = next((s for s in sites if s.site_id == session.site_id), None)
    if session.kind == "account" and (
        active is None
        or not any(
            g.site_id == session.site_id and g.access == "account" for g in policy.site_grants
        )
    ):
        return _no("session_mismatch", "request_refused")
    login = _lease(policy.login_lease, 'login', policy)
    manual = _lease(policy.manual_lease, 'manual', policy)
    authorization = _authorization_query(request)
    if infra_match(request, policy, extra_hosts) and not (login and authorization[3] and not manual):
        return _yes("infrastructure")
    if chrome_background(request):
        # Chrome's own telemetry and background checks: refused.
        return _no("browser_background", "request_refused")
    if hop:
        return _yes("download_hop")
    if session.kind == "public":
        return _yes("public_session")
    destinations = [s for s in sites if origin in (*s.origins, *s.login_origins)]
    provider_sites = [
        s for s in sites if any(b.provider_origin == origin for b in s.provider_bindings)
    ]
    if (not destinations and not (active in provider_sites) and not (login and authorization[3])
            and browser_initiated(request)):
        # The browser's or the extension's own call off the site set (casa-test F10).
        return _no("browser_background", "request_refused")
    if manual and destinations:
        return _yes("manual_lease")
    if login:
        parts, pairs, names, shaped = authorization
        if shaped:
            exact = (names.count('client_id') == names.count('redirect_uri') == 1
                     and sum(name == 'client_id' for name, _, _ in pairs) == 1
                     and sum(name == 'redirect_uri' for name, _, _ in pairs) == 1)
            if exact and tagger is not None and within_shape(request, PROVIDER_SHAPE):
                values = {name: value for name, _, value in pairs}
                try:
                    tags = (tagger('path', (parts.path or '/').encode()),
                            tagger('client_id', values['client_id'].encode()),
                            tagger('redirect_uri', values['redirect_uri'].encode()))
                    if any(b.provider_origin == origin and
                           (b.path_tag, b.client_id_tag, b.redirect_uri_tag) == tags
                           for b in active.provider_bindings):
                        return _yes('login_lease')
                except Exception:
                    pass
            return _no('provider_binding')
        if origin in active.login_origins:
            return _yes("login_lease")
    if request.resource_type == "document" or _unclassified(request):
        # The one rule of the account session's reach: its top-level documents stay on the
        # site. The agent's own navigations off it moved to the public session before.
        if origin in (*active.origins, *active.login_origins):
            return _yes("site_document")
        return _no("site_lock", "request_refused")
    return _yes("site_page")
