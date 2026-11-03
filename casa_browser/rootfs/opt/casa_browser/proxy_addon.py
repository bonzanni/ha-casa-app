"""The enforcing proxy's decision layer: a mitmproxy addon (Plan 1 Task 6).

Every request on the **browser listener** is decided by ``policy.decide`` against the policy
the controller installed last, after the body has arrived and before any upstream byte
(mitmproxy runs with ``connection_strategy=lazy``). A refused request is answered locally with
an empty ``403``. An allowed one leaves a *pin*: the addresses the decision checked, which the
proxy's event loop (``PinnedLoop`` in ``proxy_main``) is the only source of when mitmproxy
dials the host — so a second DNS answer is never consulted and a name without a pin cannot be
dialled at all. The proxy holds no policy at start: everything is refused until one is
installed.

The **broker listener** carries only the broker's 1Password SDK traffic: ``CONNECT`` to a
listed host on port 443, intercepted, its TLS server name and every ``Host`` equal to the
``CONNECT`` host. No run policy applies there.

The **download listener** carries only the controller's downloader (``downloads.PROXY_URL``):
its requests are decided like the browser's, as download hops (``decide(…, hop=True)``: the
floor, the infrastructure rules and Chrome's background refusals, DNS pinning included; never
the account session's document rule), so an off-site CDN hop of a link download is fetched.

Nothing here logs a request: decisions become ``PrivateRequestEvent`` records in a bounded
ring that only the controller reads.
"""
from __future__ import annotations

import collections
import dataclasses
import functools
import inspect
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import time
from typing import Awaitable, Callable
from urllib.parse import unquote_plus, urlsplit

from mitmproxy import http

from casa_browser import infra, netfloor
from casa_browser.identity import SEMANTIC_HEADERS, IdentityError, address
from casa_browser.netfloor import FloorError, ResolvedTarget
from casa_browser.policy import Decision, Lease, RequestMeta, RunPolicy, decide, https_upgrade
from casa_browser.proxy_client import PrivateRequestEvent

MAX_REQUEST_BODY = 1024 * 1024        # a request body's bound; larger is refused
EVENT_RING = 10_000
EVENT_BYTES = 64 * 1024 * 1024  # the ring's total size
EVENT_OVERHEAD = 512             # counted per event besides its origin


def _event_size(e: PrivateRequestEvent) -> int:
    return EVENT_OVERHEAD + len(e.origin or "")


WINDOW_QUIET = 3.0
EVENT_METHODS = frozenset('GET HEAD POST PUT PATCH DELETE OPTIONS CONNECT'.split())


def _binding_key(path: Path | None) -> bytes:
    if path is None:
        return secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        key = path.read_bytes()
        if len(key) == 32:
            os.chmod(path, 0o600)
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    temporary = path.with_name('.binding-' + secrets.token_hex(16))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return key


def _bodiless_by_construction(req) -> bool:
    """A request a browser cannot attach a body to: GET and HEAD (the Fetch standard forbids
    a body on them) and a CORS preflight, an OPTIONS carrying ``access-control-request-method``,
    a header pages cannot set. Every other request must declare its size."""
    return req.method in ("GET", "HEAD") or (
        req.method == "OPTIONS" and "access-control-request-method" in req.headers)
PIN_TTL = 60.0

_DEST_TYPES = {
    "document": "document", "iframe": "subframe", "frame": "subframe", "embed": "subframe",
    "object": "subframe", "script": "script", "serviceworker": "script",
    "sharedworker": "script", "worker": "script", "style": "stylesheet", "image": "image",
    "font": "font", "audio": "media", "video": "media", "track": "media",
}


def classify(method: str, headers) -> tuple[str, bool, str | None]:
    """(resource_type, is_navigation, preflight_method) from what the browser itself sets.
    A page cannot set ``Sec-Fetch-*`` (forbidden header names); a request without them is
    ``other`` — the class that is always a send under account rules."""
    if headers.get("upgrade", "").lower() == "websocket":
        return "websocket", False, None
    dest = headers.get("sec-fetch-dest", "").lower()
    resource = _DEST_TYPES.get(dest, "other")
    navigation = headers.get("sec-fetch-mode", "").lower() == "navigate" and resource in (
        "document", "subframe")
    preflight = None
    if method == "OPTIONS" and "access-control-request-method" in headers:
        preflight = headers["access-control-request-method"]
    return resource, navigation, preflight


def request_meta(method: str, url: str, body: bytes, headers: dict) -> RequestMeta:
    """The policy's view of one request, from what the browser sent (``headers`` lowercased):
    its class, whether it navigates, a preflight's method, and whether the browser made it on
    its own — no ``Sec-Fetch-*`` header at all, or ``Sec-Fetch-Site: none`` on a request that
    is not a navigation (measured: Chrome's own background requests carry ``Sec-Fetch-Site:
    none``, ``Sec-Fetch-Mode: no-cors``, ``Sec-Fetch-Dest: empty``; a page cannot send ``none``,
    which only a navigation the user starts carries)."""
    resource, navigation, preflight = classify(method, headers)
    return RequestMeta(method=method, url=url, body=body, content_type=headers.get("content-type"),
                       headers=headers, resource_type=resource, is_navigation=navigation,
                       preflight_method=preflight,
                       browser_own=_browser_own(headers))


def _browser_own(headers: dict) -> bool:
    if not any(k.startswith("sec-fetch-") for k in headers):
        return True
    return (headers.get("sec-fetch-site", "").lower() == "none"
            and headers.get("sec-fetch-mode", "").lower() != "navigate")


def fail_closed(phase: str):
    """mitmproxy logs an exception raised in an addon hook and then carries on — so a hook
    that raises would let its request through undecided. Every hook here fails closed instead:
    a request-phase hook refuses the flow, a later one closes the client connection (and drops
    a pending WebSocket frame), ``server_connect`` refuses the dial. The exception text is
    never logged (it can hold request bytes)."""

    def wrap(fn):
        def on_error(self, arg):
            try:
                if phase == "connect":
                    arg.server.error = "casa: refused"
                    return
                if phase == "request" and arg.response is None:
                    self._refuse(arg)
                if phase == "frame" and getattr(arg, "websocket", None) and arg.websocket.messages:
                    arg.websocket.messages[-1].drop()
                self._record("browser", Decision(False, "internal", "request_refused"),
                             getattr(getattr(arg, "request", None), "method", "?"), flow=arg)
                self._close_client(arg.client_conn.id)
            except Exception:
                pass

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def run(self, arg):
                try:
                    return await fn(self, arg)
                except Exception:
                    on_error(self, arg)
        else:
            @functools.wraps(fn)
            def run(self, arg):
                try:
                    return fn(self, arg)
                except Exception:
                    on_error(self, arg)
        return run
    return wrap


@dataclasses.dataclass
class _Socket:
    flow: http.HTTPFlow
    meta: RequestMeta
    epoch: int


class Pins:
    """host:port → checked addresses, filled only by an allowing decision."""

    def __init__(self, clock=time.monotonic):
        self._pins: dict[tuple[str, int], tuple[tuple, float]] = {}
        self._clock = clock

    MAX = 4096  # pins kept; a page naming endless distinct hosts must not grow the table

    def put(self, target: ResolvedTarget) -> None:
        key = (target.host, target.port)
        self._pins.pop(key, None)
        self._pins[key] = (target.addresses, self._clock() + PIN_TTL)
        if len(self._pins) > self.MAX:
            now = self._clock()
            for k in [k for k, (_, until) in self._pins.items() if until < now]:
                del self._pins[k]
            while len(self._pins) > self.MAX:   # all fresh: the oldest go (they re-resolve)
                del self._pins[next(iter(self._pins))]

    def fresh(self, host: str, port: int) -> tuple | None:
        """The checked addresses for host:port while the pin is fresh, else ``None``."""
        entry = self._pins.get((host.lower().rstrip("."), port))
        if entry is None or entry[1] < self._clock():
            return None
        return entry[0]


def _reach(policy: RunPolicy) -> tuple:
    """What ``policy.decide`` reads of a policy, its sites' provider bindings aside."""
    return (policy.run_id, policy.mode, policy.site_grants, policy.login_lease,
            policy.manual_lease, policy.active_session,
            tuple((s.site_id, s.origins, s.login_origins) for s in policy.sites))


def _only_adds_bindings(old: RunPolicy | None, new: RunPolicy) -> bool:
    """*new* is *old* with provider bindings added and nothing else ``decide`` reads changed:
    a binding only widens what a policy admits, so *new* refuses nothing *old* admitted."""
    return (old is not None and _reach(old) == _reach(new)
            and all(b in n.provider_bindings
                    for o, n in zip(old.sites, new.sites) for b in o.provider_bindings))


class EnforcingAddon:
    def __init__(self, *, resolver: Callable[[str, int], Awaitable] = netfloor.default_resolver,
                 broker_hosts: frozenset[str] = frozenset(), extension_hosts: frozenset[str] = frozenset(),
                 browser_port: int = netfloor.PROXY_BROWSER_PORT,
                 broker_port: int = netfloor.PROXY_BROKER_PORT,
                 download_port: int = netfloor.PROXY_DOWNLOAD_PORT,
                 clock=time.time, binding_key_path=None, tagger=None):
        self.binding_key_path = Path(binding_key_path) if binding_key_path is not None else None
        self._key = _binding_key(self.binding_key_path)
        # Names the key without revealing it: a provider binding learned under another key
        # (an HA restore makes a new one, ``proxy/*`` is not backed up) is stale.
        self.key_id = hmac.new(self._key, b'casa-browser binding key id', hashlib.sha256).hexdigest()[:16]
        self.tagger = tagger if tagger is not None else self._tag
        self.resolver = resolver
        self.broker_hosts = frozenset(broker_hosts) | infra.BROKER_SDK_HOSTS
        self.extension_hosts = frozenset(extension_hosts)
        self.browser_port = browser_port
        self.broker_port = broker_port
        self.download_port = download_port
        self.clock = clock
        self._nonce = secrets.token_hex(16)
        self._counter = 0
        self.last_close_all = self.clock()
        self.policy = None
        self.open_window()
        self.manual_purpose = None
        self._candidates = []
        self.policy: RunPolicy | None = None
        self.epoch = 0  # bumped on every policy or lease change
        self.login_close_armed_at: float | None = None
        # Slice 5r: a login or manual lease admitted a request the policy without leases
        # refuses, and no close of every connection has happened since.
        self.lease_only_admitted = False
        self.pins = Pins()
        self.events: collections.deque[PrivateRequestEvent] = collections.deque()
        self.event_bytes = 0
        self.seq = 0
        self.dropped = 0
        self.sockets: dict[str, _Socket] = {}
        self.connections = None  # proxyserver's live connection handlers (attach())

    def attach(self, proxyserver) -> None:
        """Give the addon the running proxyserver, whose handlers own the sockets."""
        self.connections = proxyserver.connections

    # ------------------------------------------------------------------ closing sockets
    # ``flow.kill()`` does not stop a flow in transit in mitmproxy 12 (its own comment cites
    # issue 4711): a request admitted earlier is still sent when its upstream connection
    # completes, and WebSocket frames keep flowing. Authority is therefore withdrawn at the
    # socket: the client connection's handler closes every transport it owns — the browser's
    # connection, its upstream connections and any upstream connection still being opened —
    # before a control call replies.

    def _close_client(self, client_id: str) -> None:
        handler = self.connections.get(client_id) if self.connections is not None else None
        if handler is None:
            return
        for conn, io in list(handler.transports.items()):
            if io.handler is not None:
                handler.close_connection(conn)

    def _close_browser_connections(self) -> None:
        if self.connections is None:
            return
        for client_id, handler in list(self.connections.items()):
            sockname = getattr(handler.client, "sockname", None)
            if sockname and sockname[1] in (self.browser_port, self.download_port):
                self._close_client(client_id)
        self.sockets.clear()

    # ------------------------------------------------------------------ control operations

    def _changed(self, policy: RunPolicy | None, *, close_all: bool, purpose: str | None) -> None:
        """The one policy transition: replace, invalidate every cached decision, withdraw.

        ``purpose`` is the manual lease's purpose after the change."""
        lease = policy.manual_lease if policy is not None else None
        self.policy = policy
        self.manual_purpose = purpose if lease is not None else None
        self.epoch += 1
        if close_all:
            # Slice 5r: the one discharge of a lease-only admission is a close of every
            # connection; a lease that ends without one (``responseheaders``) keeps the mark.
            self.lease_only_admitted = False
            self._authority_closed()
            self._close_browser_connections()
        else:
            self._recheck_sockets()

    def install(self, policy: RunPolicy) -> int:
        """Replace the policy in one assignment and close every browser connection, so no
        request admitted under the old policy can still be sent once this returns — unless the
        new policy only adds provider bindings to the old one (a binding a live view's sign-in
        taught), which refuses nothing the old one admitted: closing would only cut the page
        the operator just signed into while it still loads."""
        same = (self.policy is not None and self.policy.run_id == policy.run_id
                and self.policy.active_session == policy.active_session
                and self.policy.manual_lease == policy.manual_lease)
        widens = _only_adds_bindings(self.policy, policy)
        self._changed(policy, close_all=not widens,
                      purpose=self.manual_purpose if same else None)
        if widens:
            self._authority_closed()
        self.login_close_armed_at = None
        if not same:
            self._candidates.clear()
        return policy.revision

    def revoke(self, run_id: str) -> None:
        if self.policy is None or self.policy.run_id == run_id:
            self.login_close_armed_at = None
            self._candidates.clear()
            self._changed(None, close_all=True, purpose=None)

    def _lease(self, field_name: str, lease: Lease | None, *, close_all: bool,
               purpose: str | None) -> None:
        if self.policy is None:
            raise LookupError("no_policy")
        if lease is not None and lease.run_id != self.policy.run_id:
            raise LookupError("wrong_run")
        self._changed(dataclasses.replace(self.policy, **{field_name: lease}), close_all=close_all,
                      purpose=purpose)

    def begin_manual(self, lease: Lease, *, purpose: str):
        if purpose not in ('login', 'assist'):
            raise ValueError('unsupported')
        if lease.kind != "manual":
            raise LookupError("wrong_kind")
        self._lease("manual_lease", lease, close_all=False, purpose=purpose)
        self._candidates.clear()
        return self.open_window()

    def end_manual(self, lease_id: str) -> None:
        """Done in the live view. Like ``end_login``: every connection is closed only when a
        lease admitted a request the policy without leases refuses (a document on another
        granted site's origin) and no close of every connection happened since; otherwise the
        page the operator leaves keeps loading."""
        if self.policy and self.policy.manual_lease and self.policy.manual_lease.lease_id == lease_id:
            close_all = self.lease_only_admitted
            self._lease("manual_lease", None, close_all=close_all, purpose=None)
            if not close_all:
                self._authority_closed()
            self._candidates.clear()

    def begin_login(self, lease: Lease):
        if lease.kind != "login":
            raise LookupError("wrong_kind")
        self._lease("login_lease", lease, close_all=False, purpose=self.manual_purpose)
        self.login_close_armed_at = None  # only once granted: a refused call changes nothing
        return self.open_window()

    def arm_login_close(self, lease_id: str) -> None:
        if not (self.policy and self.policy.login_lease
                and self.policy.login_lease.lease_id == lease_id):
            raise LookupError("no_lease")
        self.login_close_armed_at = self.clock()

    def end_login(self, lease_id: str) -> None:
        """Slice 5r: once ``end_login`` returns, no request a login lease alone admitted is still
        sent. Every connection is closed when a login lease admitted a request the policy without
        it refuses (a provider's sign-in page) and no close of every connection happened since,
        whichever lease this call names: one closed at its landing document (``responseheaders``,
        which closes nothing) or replaced meanwhile included. Otherwise every request admitted
        under a lease is one the policy after it admits as well, and closing would only cut a
        page still loading — measured: a sign-in page's own scripts and calls cut right after
        its first load, leaving it on a spinner for good."""
        current = bool(self.policy and self.policy.login_lease
                       and self.policy.login_lease.lease_id == lease_id)
        close_all = self.lease_only_admitted
        if current:
            self.login_close_armed_at = None
            self._lease("login_lease", None, close_all=close_all, purpose=self.manual_purpose)
            if not close_all:
                self._authority_closed()
        elif close_all and self.policy is not None:
            # The lease already ended without a close: the close it owes happens now.
            self._changed(self.policy, close_all=True, purpose=self.manual_purpose)

    def events_after(self, after: int) -> list[PrivateRequestEvent]:
        return [e for e in self.events if e.seq > after]

    # ------------------------------------------------------------------ helpers

    def _record(self, listener, decision: Decision, method, origin=None, resource="other",
                navigation=False, *, flow=None, binding=None):
        if self._withhold(flow):
            origin = origin if self._known_origin(origin) else None
            method = method if method in EVENT_METHODS else 'OTHER'
            resource = resource if resource in {*_DEST_TYPES.values(), 'other', 'websocket'} else 'other'
        self.seq += 1
        policy = self.policy
        event = PrivateRequestEvent(
            seq=self.seq, run_id=policy.run_id if policy else None,
            revision=policy.revision if policy else None, listener=listener,
            allow=decision.allow, reason=decision.reason, event_kind=decision.event_kind,
            method=method, origin=origin, resource_type=resource,
            is_navigation=navigation, at=self.clock(), binding=binding)
        self.events.append(event)
        self.event_bytes += _event_size(event)
        # Bounded by count and by bytes; the oldest go first and are counted, never silently.
        while len(self.events) > EVENT_RING or (self.event_bytes > EVENT_BYTES and len(self.events) > 1):
            self.event_bytes -= _event_size(self.events.popleft())
            self.dropped += 1

    def _known_origin(self, origin):
        p = self.policy
        if p is None or origin is None:
            return False
        if any(origin in (*s.origins, *s.login_origins) for s in p.sites):
            return True
        hosts = self.extension_hosts | self.broker_hosts | infra.ONEPASSWORD_EXTENSION_HOSTS
        hosts |= frozenset(r.host for r in infra.INFRA_RULES if r.host is not None)
        return origin in {'https://' + host for host in hosts}

    def _withhold(self, flow=None):
        return self.window_open or bool(flow is not None and flow.metadata.get('casa_window_open'))

    @property
    def window_open(self):
        return self.clock() < self.window_until

    def _authority_closed(self):
        self.last_close_all = self.clock()
        if self.last_close_all < self.window_until < float('inf'):
            self.window_until = max(self.window_until, self.last_close_all + WINDOW_QUIET)

    def _tag(self, label, raw):
        return hmac.new(self._key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()

    def _listener(self, flow) -> str | None:
        port = flow.client_conn.sockname[1] if flow.client_conn.sockname else None
        if port == self.browser_port:
            return "browser"
        if port == self.broker_port:
            return "broker"
        if port == self.download_port:
            return "download"
        return None

    @staticmethod
    def _refuse(flow, status=403):
        flow.response = http.Response.make(status, b"", {"content-type": "text/plain",
                                                          "cache-control": "no-store"})

    def _decide(self, meta: RequestMeta, *, hop: bool = False) -> Decision:
        return decide(meta, self.policy, extra_hosts=self.extension_hosts, tagger=self.tagger,
                      hop=hop)

    def _lease_only(self, meta: RequestMeta, hop: bool) -> bool:
        """A lease is what admits *meta*: the policy without its login and manual leases
        refuses it."""
        p = self.policy
        return p is not None and (p.login_lease is not None or p.manual_lease is not None) and not decide(
            meta, dataclasses.replace(p, login_lease=None, manual_lease=None),
            extra_hosts=self.extension_hosts, tagger=self.tagger, hop=hop).allow

    def _recheck_sockets(self):
        for key, sock in list(self.sockets.items()):
            if not self._decide(sock.meta).allow:
                del self.sockets[key]
                self._close_client(sock.flow.client_conn.id)
            else:
                sock.epoch = self.epoch

    async def _target(self, host: str, port: int) -> ResolvedTarget | None:
        """A fresh pin, or one new resolution checked by the floor (``None`` when refused)."""
        name = host.lower().rstrip(".")
        pinned = self.pins.fresh(name, port)
        if pinned is not None:
            return ResolvedTarget(name, port, pinned)
        try:
            target = await netfloor.resolve_public(host, port, self.resolver)
        except FloorError:
            return None
        self.pins.put(target)
        return target

    # ------------------------------------------------------------------ mitmproxy hooks

    @fail_closed("request")
    def http_connect(self, flow: http.HTTPFlow) -> None:
        listener = self._listener(flow)
        host = flow.request.host
        if listener == "broker":
            if flow.request.port != 443 or netfloor.valid_hostname(host) not in self.broker_hosts:
                self._refuse(flow)
                self._record("broker", Decision(False, "broker_host", "request_refused"), "CONNECT")
            return
        if listener not in ("browser", "download"):
            self._refuse(flow)
            return
        literal = host[1:-1] if host.startswith("[") else host
        if netfloor._parse_ip(literal) is not None:
            if not netfloor.is_public_address(literal):
                self._refuse(flow)
                self._record(listener, Decision(False, "floor", "request_refused"), "CONNECT")
        elif netfloor.valid_hostname(host) is None:
            self._refuse(flow)
            self._record(listener, Decision(False, "floor", "request_refused"), "CONNECT")

    @fail_closed("request")
    def requestheaders(self, flow: http.HTTPFlow) -> None:
        flow.metadata['casa_window_open'] = self.window_open
        if self.policy and self.policy.manual_lease:
            flow.metadata['casa_manual_lease'] = self.policy.manual_lease.lease_id
        # Our decision needs the whole body; never stream a request.
        flow.request.stream = False
        req = flow.request
        length = req.headers.get("content-length")
        status = None
        if "transfer-encoding" in req.headers:
            status = 411  # a chunked HTTP/1.1 body has no declared size to bound
        elif length is not None and (not (length.isascii() and length.isdigit())
                                     or int(length) > MAX_REQUEST_BODY):
            status = 413
        elif (length is None and not req.http_version.startswith("HTTP/1")
              and not _bodiless_by_construction(req)):
            # HTTP/2 or later with no declared size: possibly a streamed upload, which
            # mitmproxy would buffer whole before any decision.
            status = 411
        if status is not None:
            # Refuse, and kill: a response set here does not end mitmproxy's body phase, so
            # without the kill it would still read the whole body into memory.
            self._refuse(flow, status)
            flow.kill()
            self._record(self._listener(flow) or "browser",
                         Decision(False, "body_bound", "request_refused"), req.method, flow=flow)

    @fail_closed("request")
    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None or flow.request.method == "CONNECT":
            return
        listener = self._listener(flow)
        if listener == "broker":
            await self._broker_request(flow)
            return
        if listener not in ("browser", "download"):
            self._refuse(flow)
            return
        hop = listener == "download"
        meta = self._meta(flow)
        if meta is None:
            self._refuse(flow)
            self._record(listener, Decision(False, "unidentifiable", "request_refused"),
                         flow.request.method, flow=flow)
            return
        first = self._decide(meta, hop=hop)
        target = None
        if first.allow:
            target = await self._target(flow.request.host, flow.request.port)
            if target is None:
                first = Decision(False, "floor", "request_refused")
        decision = first
        if target is not None:
            meta = dataclasses.replace(meta, resolved_addresses=tuple(str(a) for a in target.addresses))
            # The policy may have changed while resolving: decide again, on the current one.
            decision = self._decide(meta, hop=hop)
        upgrade = None if (decision.allow or hop) else https_upgrade(meta, self.policy)
        if upgrade is not None:
            decision = Decision(False, "https_upgrade", "request_refused")
        if decision.allow and self._lease_only(meta, hop):
            self.lease_only_admitted = True
        self._record_request(meta, decision, flow=flow, listener=listener)
        if upgrade is not None:
            # Answered here, never upstream: the browser asks again over https (casa-test F16).
            flow.response = http.Response.make(307, b"", {"location": upgrade,
                                                          "cache-control": "no-store"})
            return
        if not decision.allow:
            self._refuse(flow)
            return
        flow.metadata["casa_epoch"] = self.epoch
        if meta.resource_type == "websocket":
            self.sockets[flow.id] = _Socket(flow, meta, self.epoch)

    def _record_request(self, meta: RequestMeta, decision: Decision, *, flow=None,
                        listener: str = "browser") -> None:
        origin = binding = None
        try:
            origin = address(meta.url)[0]
            if self._withhold(flow) and listener == "browser":
                binding = self._binding_projection(meta, flow)
        except IdentityError:
            pass
        self._record(listener, decision, meta.method, origin, meta.resource_type,
                     meta.is_navigation, flow=flow, binding=binding)

    def _binding_projection(self, meta, flow):
        p = self.policy
        lease = p.manual_lease if p else None
        if (self.manual_purpose != 'login' or lease is None or lease.kind != 'manual'
                or lease.run_id != p.run_id or lease.site_id != p.active_session.site_id
                or lease.generation != p.active_session.generation
                or meta.resource_type != 'document'
                or (flow is not None and flow.metadata.get('casa_manual_lease') != lease.lease_id)):
            return None
        origin, path = address(meta.url)[:2]
        callback_binding = None
        for number, callback_origin, callback_path in self._candidates:
            if (origin, path or '/') == (callback_origin, callback_path):
                callback_binding = {'callback_of': number}
                break
        active = next((s for s in p.sites if s.site_id == lease.site_id), None)
        if meta.method != 'GET' or active is None or origin not in active.login_origins:
            return callback_binding
        pairs = [member.partition('=') for member in urlsplit(meta.url).query.split('&')]
        try:
            if any(re.search(r'%(?![0-9a-fA-F]{2})', name) for name, _, _ in pairs):
                return callback_binding
            names = [unquote_plus(name, errors='strict') for name, _, _ in pairs]
            if any(names.count(key) != 1 or sum(n.lower() == key for n in names) != 1
                   for key in ('client_id', 'redirect_uri')):
                return callback_binding
            values = dict(zip(names, (value for _, _, value in pairs)))
            raw = {'path': (path or '/').encode(), 'client_id': values['client_id'].encode(),
                   'redirect_uri': values['redirect_uri'].encode()}
        except (ValueError, UnicodeError, IdentityError):
            return callback_binding
        callback_origin = callback_path = None
        try:
            callback = unquote_plus(values['redirect_uri'], errors='strict')
            callback_origin, callback_path = address(callback)[:2]
            callback_path = callback_path or '/'
        except (ValueError, UnicodeError, IdentityError):
            pass
        number = len(self._candidates) + 1
        self._candidates.append((number, callback_origin, callback_path))
        return {'candidate': number, **{label + '_tag': self.tagger(label, value)
                                      for label, value in raw.items()}}

    def _meta(self, flow: http.HTTPFlow) -> RequestMeta | None:
        req = flow.request
        try:
            path = req.data.path.decode("ascii")
        except UnicodeDecodeError:
            return None
        host = req.host.lower().strip("[]")
        # The tunnel, the TLS server name and the Host header must name one host.
        authority = req.host_header
        if authority is None:
            return None
        if not self._names_host(authority, host, req.port):
            return None
        # An absolute-form request target is a fourth name for the host: it must agree too
        # (an HTTP/1.1 server prefers it over Host).
        if req.authority and not self._names_host(req.authority, host, req.port):
            return None
        sni = flow.client_conn.sni
        if req.scheme == "https" and sni is not None and sni.lower() != host:
            return None
        semantic = [k.lower() for k in req.headers.keys() if k.lower() in SEMANTIC_HEADERS]
        if len(semantic) != len(set(semantic)):
            return None
        headers = {k.lower(): v for k, v in req.headers.items()}
        default = 443 if req.scheme == "https" else 80
        netloc = host if req.port == default else f"{host}:{req.port}"
        return request_meta(req.method, f"{req.scheme}://{netloc}{path}", req.raw_content or b"",
                            headers)

    @staticmethod
    def _names_host(authority: str, host: str, port: int) -> bool:
        """``host[:port]`` (or ``[v6]:port``) names exactly ``host`` and, if given, ``port``."""
        if authority.startswith("["):
            a_host, _, rest = authority[1:].partition("]")
            a_port = rest[1:] if rest.startswith(":") else ("" if not rest else None)
        else:
            a_host, sep, a_port = authority.rpartition(":")
            if not sep:
                a_host, a_port = authority, ""
        if a_port is None or (a_port and not (a_port.isascii() and a_port.isdigit())):
            return False
        return a_host.lower() == host.strip("[]") and (not a_port or int(a_port) == port)

    async def _broker_request(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        host = netfloor.valid_hostname(req.host)
        ok = (req.scheme == "https" and req.port == 443 and host in self.broker_hosts
              and (flow.client_conn.sni or "").lower() == host
              and (req.host_header or "").lower() in (host, f"{host}:443"))
        target = await self._target(host, 443) if ok else None
        ok = target is not None
        if not ok:
            self._refuse(flow)
        origin = None
        try:
            candidate = address(req.url)[0]
            # Broker traffic: its origin is disclosed only if the run itself registered
            # it, independently of the SDK's host allowlist.
            if self.policy and any(candidate in (*s.origins, *s.login_origins)
                                   for s in self.policy.sites):
                origin = candidate
        except IdentityError:
            pass
        self._record('broker', Decision(ok, 'broker_host',
                                       'request_allowed' if ok else 'request_refused'),
                     req.method, origin, flow=flow)

    @fail_closed("connect")
    def server_connect(self, data) -> None:
        """Defence in depth: a connection whose host has no fresh pin is never opened."""
        host, port = data.server.address
        literal = host[1:-1] if host.startswith("[") else host
        if netfloor._parse_ip(literal) is not None:
            if not netfloor.is_public_address(literal):
                data.server.error = "casa: not admitted"
            return
        if self.pins.fresh(host, port) is None:
            data.server.error = "casa: not admitted"

    @fail_closed("response")
    def responseheaders(self, flow: http.HTTPFlow) -> None:
        resp = flow.response
        resp.headers.pop("alt-svc", None)  # no QUIC hints: the browser reaches only this proxy
        # Every response streams: nothing here reads a response body, and a buffered one would
        # hold the whole body, several times over, until garbage collection reaches its flow.
        resp.stream = True
        # Login lease closure: the first document response after the broker's submit closes
        # the lease before any byte of the landing page reaches the browser.
        if (self.login_close_armed_at is not None and self.policy is not None
                and self.policy.login_lease is not None
                and flow.request.timestamp_start >= self.login_close_armed_at
                and classify(flow.request.method, flow.request.headers)[0] in ("document", "subframe")):
            self.login_close_armed_at = None
            self._authority_closed()
            # Not close_all: that would cut the landing document this response carries. A
            # request admitted under the lease before this point is inside the accepted
            # login window; everything decided from here on is decided without the lease.
            self._changed(dataclasses.replace(self.policy, login_lease=None), close_all=False,
                          purpose=self.manual_purpose)
            self._record("browser", Decision(True, "login_lease_closed", "login_lease_closed"),
                         flow.request.method, flow=flow)

    @fail_closed("response")
    def error(self, flow: http.HTTPFlow) -> None:
        self.sockets.pop(flow.id, None)

    @fail_closed("frame")
    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """Every frame is re-decided once per policy change; a refused frame is dropped (the
        one per-message control mitmproxy honours) and its connection closed. Only the frame
        being decided is kept: mitmproxy would otherwise hold every message of an open socket
        until it closes (its relay sends the message it holds, not the list)."""
        del flow.websocket.messages[:-1]
        sock = self.sockets.get(flow.id)
        if sock is not None and sock.epoch != self.epoch:
            if self._decide(sock.meta).allow:
                sock.epoch = self.epoch
            else:
                del self.sockets[flow.id]
                sock = None
        if sock is None:
            flow.websocket.messages[-1].drop()
            self._close_client(flow.client_conn.id)

    @fail_closed("response")
    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self.sockets.pop(flow.id, None)

    @fail_closed("response")
    def tcp_start(self, flow) -> None:
        # No opaque passthrough inside a CONNECT (rawtcp is off; this is the second layer).
        self._close_client(flow.client_conn.id)

    @fail_closed("response")
    def udp_start(self, flow) -> None:
        self._close_client(flow.client_conn.id)

    @property
    def start_nonce(self) -> str:
        """Drawn at each proxy start (the first member of every window token)."""
        return self._nonce

    def open_window(self):
        self.window_until = float('inf')
        self._counter += 1
        self.token = (self._nonce, self._counter)
        return self.token

    def close_window(self, token):
        if tuple(token) == self.token and self.window_until == float('inf'):
            self.window_until = max(self.clock(), self.last_close_all) + WINDOW_QUIET
            self._recheck_sockets()
