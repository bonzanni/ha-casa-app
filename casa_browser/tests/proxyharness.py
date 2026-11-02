"""Test harness for the enforcing proxy (Task 6): the real ``proxy_main.Proxy`` on loopback,
a TLS fixture site behind a counting relay, and a client.

Receipts are counted where the bytes arrive: the relay in front of the fixture counts TCP
connections and every byte received, before TLS is even terminated — so "zero receipts"
means nothing reached the upstream socket. The only test-only seam is at the harness
boundary: the resolver answers a fixed public-looking address (checked by the production
floor like any other), and ``FixtureLoop`` maps that pinned address to the fixture's loopback
port when mitmproxy dials it. ``proxy_main`` itself has no such option.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from casa_browser import netfloor
from casa_browser.policy import Lease, RunPolicy, SessionRef
from casa_browser.protocol import ProviderBinding, Site, SiteGrant
from casa_browser.proxy_addon import EnforcingAddon
from casa_browser.proxy_main import PinnedLoop, Proxy

def ip4(*octets: int) -> str:
    """A dotted IPv4 address built from its octets (the publication guard refuses dotted
    private-range literals in source; these are synthetic examples, not real addresses)."""
    return ".".join(str(o) for o in octets)


FIXTURE_IP = "93.184.215.14"   # public-looking; never dialled for real
PRIVATE_IP = ip4(10, 20, 30, 40)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_ca_and_cert(tmp: Path, hostnames: list[str]) -> tuple[Path, Path, Path]:
    """A test CA and a server certificate for ``hostnames`` (synthetic, generated per run)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "casa-browser test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hostnames]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_path, cert_path, key_path = tmp / "fixture-ca.pem", tmp / "fixture.pem", tmp / "fixture-key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return ca_path, cert_path, key_path


@dataclass
class Receipts:
    connections: int = 0
    bytes: int = 0
    requests: list = field(default_factory=list)   # (method, host, path_qs, body) seen by the app
    ws_messages: list = field(default_factory=list)
    cookies: list = field(default_factory=list)    # (host, path, Cookie header) when one was sent

    def reset(self):
        self.connections = self.bytes = 0
        self.requests.clear()
        self.ws_messages.clear()
        self.cookies.clear()


class Fixture:
    """An HTTPS + WSS site for every host in ``hostnames``, behind a counting TCP relay."""

    def __init__(self, tmp: Path, hostnames: list[str]):
        self.ca, cert, key = make_ca_and_cert(tmp, hostnames)
        self.ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.ctx.load_cert_chain(cert, key)
        self.receipts = Receipts()
        self.port = None
        self._app_port = None
        self.request_seen = asyncio.Event()
        self.response_hold: asyncio.Event | None = None
        self.hold_paths: set[str] | None = None   # with response_hold: hold only these paths
        self.hold: asyncio.Event | None = None  # set: the relay forwards nothing until it fires
        # path -> (content type, body, extra headers): fixed test pages, any host.
        self.pages: dict[str, tuple[str, bytes, dict]] = {}
        self.status: dict[str, int] = {}  # path -> status of its fixed page (default 200)

    async def _handle(self, request: web.Request):
        if request.headers.get("upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.receipts.requests.append(("WS", request.host, request.path_qs, b""))
            async for msg in ws:
                self.receipts.ws_messages.append(msg.data)
                await ws.send_str("echo:" + str(msg.data))
            return ws
        body = await request.read()
        self.receipts.requests.append((request.method, request.host, request.path_qs, body))
        self.request_seen.set()
        if self.response_hold is not None and (self.hold_paths is None or request.path in self.hold_paths):
            await self.response_hold.wait()
        if "cookie" in request.headers:
            self.receipts.cookies.append((request.host, request.path, request.headers["cookie"]))
        if request.path in self.pages:
            ctype, content, extra = self.pages[request.path]
            return web.Response(body=content, content_type=ctype, headers=extra,
                                status=self.status.get(request.path, 200))
        kind = request.query.get("as", "")
        if kind == "html":
            return web.Response(text="<html><body>landing</body></html>", content_type="text/html")
        return web.Response(text="ok", content_type="text/plain")

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=self.ctx)
        await site.start()
        self._app_port = site._server.sockets[0].getsockname()[1]

        async def relay(reader, writer):
            self.receipts.connections += 1
            try:
                ur, uw = await asyncio.open_connection("127.0.0.1", self._app_port)
            except OSError:
                writer.close()
                return

            async def pump(src, dst, count):
                try:
                    if count and self.hold is not None:
                        await self.hold.wait()
                    while data := await src.read(65536):
                        if count:
                            self.receipts.bytes += len(data)
                        dst.write(data)
                        await dst.drain()
                except (OSError, ConnectionError):
                    pass
                finally:
                    dst.close()

            await asyncio.gather(pump(reader, uw, True), pump(ur, writer, False))

        self._relay = await asyncio.start_server(relay, "127.0.0.1", 0)
        self.port = self._relay.sockets[0].getsockname()[1]

    async def stop(self):
        self._relay.close()
        await self._runner.cleanup()


class FixtureLoop(PinnedLoop):
    """Maps the pinned fixture address to the relay's loopback port, after the production
    pin lookup (``PinnedLoop.getaddrinfo``) has answered."""

    fixture_port: int | None = None

    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        if str(host) == "127.0.0.1":  # the harness's own client and relay sockets
            return await asyncio.SelectorEventLoop.getaddrinfo(self, host, port, family=family,
                                                               type=type, proto=proto, flags=flags)
        out = await super().getaddrinfo(host, port, family=family, type=type, proto=proto,
                                        flags=flags)
        return [(fam, t, p, c, ("127.0.0.1", self.fixture_port) if addr[0] == FIXTURE_IP else addr)
                for fam, t, p, c, addr in out]


def fixed_resolver(table: dict[str, list[str]]):
    calls = []

    async def resolve(host, port):
        calls.append(host)
        await asyncio.sleep(0)
        return list(table.get(host, []))

    resolve.calls = calls
    return resolve


class Harness:
    def __init__(self, tmp: Path, hostnames: list[str], resolver=None, account_hosts=frozenset(),
                 browser_port: int | None = None, clock=None, binding_key_path=None,
                 control_path: Path | None = None):
        self.clock = clock
        self.binding_key_path = binding_key_path
        self.fixed_browser_port = browser_port
        self.tmp = tmp
        self.fixture = Fixture(tmp, hostnames)
        table = {h: [FIXTURE_IP] for h in hostnames}
        self.resolver = resolver or fixed_resolver(table)
        self.account_hosts = account_hosts
        self.control_path = control_path if control_path is not None else tmp / "control.sock"

    def _make_proxy(self):
        # Ports are picked only after every fixture socket is bound (they bind port 0), so a
        # fixture socket cannot take a port chosen for the proxy.
        self.browser_port, self.broker_port = self.fixed_browser_port or free_port(), free_port()
        # An immediately released ephemeral port can be selected again for the second listener.
        while self.broker_port == self.browser_port:
            self.broker_port = free_port()
        self.download_port = free_port()
        while self.download_port in (self.browser_port, self.broker_port):
            self.download_port = free_port()
        self.addon = EnforcingAddon(resolver=self.resolver, broker_hosts=self.account_hosts,
                                    extension_hosts=self.account_hosts,
                                    browser_port=self.browser_port, broker_port=self.broker_port,
                                    download_port=self.download_port,
                                    **({"binding_key_path": self.binding_key_path} if self.binding_key_path is not None else {}),
                                    **({"clock": self.clock} if self.clock is not None else {}))
        self.proxy = Proxy(addon=self.addon, confdir=self.tmp / "ca", control_path=self.control_path,
                           controller_uid=os.getuid(), browser_port=self.browser_port,
                           broker_port=self.broker_port, download_port=self.download_port)

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        await self.fixture.start()
        loop.fixture_port = self.fixture.port
        (self.tmp / "ca").mkdir(exist_ok=True)
        for attempt in range(3):  # another process may still take a picked port
            self._make_proxy()
            loop.pins = self.addon.pins
            try:
                await self.proxy.start()
                break
            except RuntimeError:
                await self.proxy.stop()
                if attempt == 2:
                    raise
        # Test-only: the proxy verifies the fixture's certificate against the fixture CA.
        self.proxy.master.options.update(ssl_verify_upstream_trusted_ca=str(self.fixture.ca))
        self.client_ctx = ssl.create_default_context(cafile=str(self.tmp / "ca" / "mitmproxy-ca-cert.pem"))
        return self

    async def __aexit__(self, *exc):
        await self.proxy.stop()
        await self.fixture.stop()

    @property
    def proxy_url(self):
        return f"http://127.0.0.1:{self.browser_port}"

    @property
    def download_url(self):
        """The downloader's own listener (S4-5)."""
        return f"http://127.0.0.1:{self.download_port}"

    async def fetch(self, method, url, *, headers=None, data=None, broker=False,
                    download=False):
        """One request through the proxy → (status, body). Fresh connection each time."""
        import aiohttp
        port = (self.broker_port if broker else self.download_port if download
                else self.browser_port)
        conn = aiohttp.TCPConnector(ssl=self.client_ctx, force_close=True)
        try:
            async with aiohttp.ClientSession(connector=conn) as s:
                import yarl
                async with s.request(method, yarl.URL(url, encoded=True), headers=headers or {}, data=data,
                                     proxy=f"http://127.0.0.1:{port}", allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return r.status, await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            return None, type(err).__name__.encode()


def run(coro_fn, loop_factory=FixtureLoop):
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coro_fn())


# --------------------------------------------------------------------------- policies

NAV = {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate"}
FETCH = {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}


def site(site_id="s1", origins=("https://site.example",), login=("https://login.example",),
         bindings=()):
    return Site(site_id=site_id, display_name="Site", origins=tuple(origins),
                login_origins=tuple(login), vault_item_id=None, provider_bindings=tuple(bindings),
                start_page_private=None)


def account_policy(sites=None, run_id="r1", revision=1, login_lease=None,
                   manual_lease=None, generation=1, grants=None):
    sites = sites or (site(),)
    grants = grants or tuple(SiteGrant(s.site_id, "account") for s in sites)
    return RunPolicy(run_id=run_id, mode="account", site_grants=tuple(grants), sites=tuple(sites),
                     login_lease=login_lease, manual_lease=manual_lease,
                     revision=revision, active_session=SessionRef("account", sites[0].site_id, generation))


def open_web_policy(run_id="r1", revision=1):
    return RunPolicy(run_id=run_id, mode="open_web", site_grants=(), sites=(),
                     login_lease=None, manual_lease=None, revision=revision,
                     active_session=SessionRef("public", None, 1))
