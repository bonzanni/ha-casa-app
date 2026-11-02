"""Task 6: the enforcing proxy, end to end in-process.

Each test runs the real ``proxy_main.Proxy`` (mitmproxy with ``EnforcingAddon``) against a
loopback TLS fixture whose counting relay sees every upstream connection and byte. A policy
allow/deny alone is never the assertion: refusals assert **zero upstream connections and
bytes** (``Receipts``)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import ssl

import aiohttp
import pytest

from casa_browser.policy import Lease
from casa_browser.protocol import ProviderBinding
from casa_browser.proxy_client import ControlError, ProxyClient, decode_policy, encode_policy

import proxyharness as h
from proxyharness import FETCH, NAV, Harness, account_policy, ip4, run, site

HOSTS = ["site.example", "login.example", "other.example", "provider.example",
         "acct.1password.eu", "unlisted.1password.eu"]


def nothing_reached(hz):
    r = hz.fixture.receipts
    return r.connections == 0 and r.bytes == 0 and not r.requests


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


def test_proxy_restart_requires_reinstall(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            for method, url in [("GET", "https://site.example/"), ("POST", "https://site.example/a")]:
                status, _ = await hz.fetch(method, url, headers=NAV, data=b"x" if method == "POST" else None)
                assert status == 403
            assert nothing_reached(hz)
            status = await ProxyClient(hz.control_path).status()
            assert status["revision"] is None
            # the proxy names its binding key (the controller tells a restored binding by it)
            assert status["binding_key_id"] == hz.addon.key_id
            assert [e.reason for e in hz.addon.events] == ["session_mismatch", "session_mismatch"]
    run(scenario)


def test_service_worker_post_blocked_upstream(tmp):
    """Design §4: an account session's top-level documents stay on its site. A top-level
    document POST off the site, or a request whose class the browser did not state (a worker,
    plain http: no ``Sec-Fetch-Dest``) off the site, never reaches the upstream; the site's own
    page loads."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(account_policy())
            assert (await hz.fetch("GET", "https://site.example/account", headers=NAV))[0] == 200
            hz.fixture.receipts.reset()
            ct = "application/x-www-form-urlencoded"
            assert (await hz.fetch("POST", "https://other.example/collect",
                                   headers={**NAV, "content-type": ct}, data=b"a=1"))[0] == 403
            assert (await hz.fetch("POST", "https://other.example/sw-sync", data=b"q=1"))[0] == 403
            assert (await hz.fetch("GET", "https://other.example/beacon?u=1"))[0] == 403
            assert nothing_reached(hz)
            kinds = [(e.reason, e.event_kind) for e in hz.addon.events][-3:]
            assert kinds == [("site_lock", "request_refused")] * 3
    run(scenario)


def test_account_page_traffic_reaches_any_floor_passing_origin(tmp):
    """Every other request of an account page — a fetch, a document POST on the site, a send
    with query data, off the site or on it — is the site's own traffic (``site_page`` /
    ``site_document``) and reaches the upstream with no approval."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(account_policy())
            ct = "application/x-www-form-urlencoded"
            sends = [("POST", "https://site.example/api/track", FETCH, b"{}"),
                     ("POST", "https://site.example/account", {**NAV, "content-type": ct}, b"a=1"),
                     ("POST", "https://other.example/api", FETCH, b"x=1"),
                     ("GET", "https://other.example/pixel?u=1", {"sec-fetch-dest": "image"}, None)]
            for method, url, headers, data in sends:
                assert (await hz.fetch(method, url, headers=headers, data=data))[0] == 200, url
            assert [r[0:3] for r in hz.fixture.receipts.requests] == [
                ("POST", "site.example", "/api/track"), ("POST", "site.example", "/account"),
                ("POST", "other.example", "/api"), ("GET", "other.example", "/pixel?u=1")]
            assert [e.reason for e in hz.addon.events] == [
                "site_page", "site_document", "site_page", "site_page"]
    run(scenario)


def test_prefetch_and_off_set_origin_refused(tmp):
    """An off-site top-level document — navigated or prefetched — is ``site_lock`` with
    nothing sent."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(account_policy())
            prefetch = {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-purpose": "prefetch"}
            assert (await hz.fetch("GET", "https://other.example/next", headers=prefetch))[0] == 403
            assert (await hz.fetch("GET", "https://other.example/a", headers=NAV))[0] == 403
            assert nothing_reached(hz)
            assert (await hz.fetch("GET", "https://site.example/b", headers=prefetch))[0] == 200
    run(scenario)


def test_download_listener_decides_hops(tmp):
    """S4-5: the downloader's listener decides as a download hop — floor, infrastructure and
    background only — so an off-site document hop reaches the upstream there, while the same
    request on the browser's listener is ``site_lock`` with nothing sent; the floor holds on
    both."""
    async def scenario():
        resolver = h.fixed_resolver({**{x: [h.FIXTURE_IP] for x in HOSTS},
                                     "rebind.example": [h.PRIVATE_IP]})
        async with Harness(tmp, HOSTS, resolver=resolver) as hz:
            hz.addon.install(account_policy())
            assert (await hz.fetch("GET", "https://other.example/file.pdf", headers=NAV))[0] == 403
            assert nothing_reached(hz)
            assert [(e.listener, e.reason) for e in hz.addon.events] == [("browser", "site_lock")]
            hz.addon.events.clear()
            assert (await hz.fetch("GET", "https://other.example/file.pdf", headers=NAV,
                                   download=True))[0] == 200
            assert [r[0:3] for r in hz.fixture.receipts.requests] == [
                ("GET", "other.example", "/file.pdf")]
            hz.fixture.receipts.reset()
            for url in ("https://rebind.example/x", f"https://{ip4(192, 168, 1, 20)}/x"):
                status, _ = await hz.fetch("GET", url, headers=NAV, download=True)
                assert status in (403, None), url
            assert nothing_reached(hz)
            assert all(e.listener == "download" for e in hz.addon.events)
            assert [e.reason for e in hz.addon.events if not e.allow][-2:] == ["floor", "floor"]
            # no policy: nothing passes on the downloader's listener either
            hz.addon.revoke("r1")
            hz.fixture.receipts.reset()
            assert (await hz.fetch("GET", "https://other.example/file.pdf", headers=NAV,
                                   download=True))[0] == 403
            assert nothing_reached(hz)
    run(scenario)


def test_changed_graphql_operation_reaches_the_site(tmp):
    """A site's own data request is free whatever its operation (design §4): no operation
    approval is consulted."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            url = "https://site.example/graphql"
            ct = "application/json"
            body = json.dumps({"operationName": "Export", "query": "query Export { export { url } }",
                               "variables": {}}).encode()
            hz.addon.install(account_policy())
            assert (await hz.fetch("POST", url, headers={**FETCH, "content-type": ct}, data=body))[0] == 200
            assert len(hz.fixture.receipts.requests) == 1
    run(scenario)


def test_private_destinations_refused_before_any_connection(tmp):
    """A redirect target or a link to the LAN arrives as a new request: an IP literal, or a
    name that resolves to a private address, is refused with nothing dialled."""
    async def scenario():
        resolver = h.fixed_resolver({"site.example": [h.FIXTURE_IP], "rebind.example": [h.PRIVATE_IP],
                                     "mixed.example": [h.FIXTURE_IP, ip4(192, 168, 1, 20)]})
        async with Harness(tmp, HOSTS, resolver=resolver) as hz:
            hz.addon.install(h.open_web_policy())
            for url in [f"https://{ip4(192, 168, 1, 20)}/", f"http://{ip4(172, 30, 32, 1)}:8123/", "https://[::1]/",
                        "https://rebind.example/", "https://mixed.example/", "http://homeassistant:8123/",
                        "https://supervisor/core/api"]:
                status, _ = await hz.fetch("GET", url, headers=NAV)
                assert status in (403, None), url
            assert nothing_reached(hz)
            assert (await hz.fetch("GET", "https://site.example/", headers=NAV))[0] == 200
    run(scenario)


def test_policy_install_atomic(tmp):
    """A request allowed by the old policy while its host resolves is decided again on the
    policy current when the answer arrives: a revocation in that window holds."""
    gate = asyncio.Event()

    async def slow(host, port):
        await gate.wait()
        return [h.FIXTURE_IP]

    async def scenario():
        async with Harness(tmp, HOSTS, resolver=slow) as hz:
            hz.addon.install(h.open_web_policy(revision=1))
            pending = asyncio.create_task(hz.fetch("GET", "https://site.example/", headers=NAV))
            for _ in range(200):
                if hz.addon.events or getattr(slow, "_seen", False):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)  # the request is now waiting on the resolver
            await ProxyClient(hz.control_path).revoke("r1")
            gate.set()
            status, _ = await pending
            assert status == 403
            assert nothing_reached(hz)
            assert hz.addon.events[-1].reason == "session_mismatch"
    run(scenario)


def test_admitted_request_not_sent_after_revocation(tmp):
    """Admitted, then its upstream connection stalls; the revocation is acknowledged during
    the stall; when the upstream answers, the request is not sent (Astra arch-r1 S1)."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.fixture.hold = asyncio.Event()
            await ProxyClient(hz.control_path).install(h.open_web_policy())
            pending = asyncio.create_task(hz.fetch("POST", "https://site.example/send", data=b"x=1"))
            for _ in range(300):
                if hz.fixture.receipts.connections:
                    break
                await asyncio.sleep(0.01)
            assert hz.fixture.receipts.connections == 1  # upstream dialled, handshake held
            await ProxyClient(hz.control_path).revoke("r1")
            hz.fixture.hold.set()
            status, _ = await pending
            await asyncio.sleep(0.5)
            assert status != 200
            assert hz.fixture.receipts.requests == []
    run(scenario)


def test_revocation_closes_open_websocket(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            wss = "wss://site.example/live"
            allowed = account_policy()
            await ProxyClient(hz.control_path).install(allowed)
            conn = aiohttp.TCPConnector(ssl=hz.client_ctx)
            async with aiohttp.ClientSession(connector=conn) as s:
                ws = await s.ws_connect(wss, proxy=hz.proxy_url, timeout=aiohttp.ClientWSTimeout(ws_receive=5))
                await ws.send_str("one")
                assert (await ws.receive(timeout=5)).data == "echo:one"
                await ProxyClient(hz.control_path).install(account_policy(revision=2))
                try:
                    await ws.send_str("two")
                    msg = await ws.receive(timeout=3)
                    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                        aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR)
                except (ConnectionError, asyncio.TimeoutError, aiohttp.ClientError):
                    pass
                await asyncio.sleep(0.3)
                assert hz.fixture.receipts.ws_messages == ["one"]
                assert ws.closed or ws.close_code is not None or msg.type != aiohttp.WSMsgType.TEXT
    run(scenario)


def test_provider_binding_exact(tmp):
    """Under a login lease the provider's authorization endpoint is reached only by the
    request carrying the recorded client id and callback, each once and byte-equal."""
    lease = Lease("L1", "r1", "s1", 1, "login")

    async def scenario():
        async with Harness(tmp, HOSTS, binding_key_path=tmp / 'proxy' / 'binding.key') as hz:
            key = hz.addon.binding_key_path.read_bytes()
            tagger = lambda label, raw: hmac.new(key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()
            binding = ProviderBinding('https://provider.example',
                path_tag=tagger('path', b'/o/oauth2/auth'),
                client_id_tag=tagger('client_id', b'client-123'),
                redirect_uri_tag=tagger('redirect_uri', b'https://login.example/callback'))
            s = site(bindings=(binding,))
            wire = encode_policy(account_policy(sites=(s,), login_lease=lease))
            expected = {'provider_origin': binding.provider_origin, 'path_tag': binding.path_tag,
                        'client_id_tag': binding.client_id_tag, 'redirect_uri_tag': binding.redirect_uri_tag}
            assert wire['sites'][0]['provider_bindings'] == [expected]
            hz.addon.install(account_policy(sites=(s,), login_lease=lease))
            base = "https://provider.example/o/oauth2/auth?"
            bad = [
                base + "client_id=other&redirect_uri=https://login.example/callback",
                base + "client_id=client-123&client_id=client-123&redirect_uri=https://login.example/callback",
                base + "client_id=client-123&redirect_uri=https%3A%2F%2Flogin.example%2Fcallback",
                base + "client_id=client-123&redirect_uri=https://login.example/other",
                "https://provider.example/o/oauth2/token?client_id=client-123&redirect_uri=https://login.example/callback",
            ]
            for url in bad:
                assert (await hz.fetch("GET", url, headers=NAV))[0] == 403, url
            assert (await hz.fetch("GET", base + "client_id=client-123&redirect_uri=https://login.example/callback",
                                   headers={**NAV, "x-http-method-override": "POST"}))[0] == 403
            assert nothing_reached(hz)
            good = base + "scope=email&client_id=client-123&redirect_uri=https://login.example/callback&state=xyz"
            assert (await hz.fetch("GET", good, headers=NAV))[0] == 200
            assert len(hz.fixture.receipts.requests) == 1
            assert hz.fixture.receipts.connections > 0 and hz.fixture.receipts.bytes > 0
            for event in hz.addon.events:
                encoded = json.dumps(encode_event(event))
                for raw in ('/o/oauth2/auth', 'client-123', 'https://login.example/callback'):
                    assert raw not in encoded
            # Outside a login lease the same request is an ordinary refused navigation.
            hz.addon.install(account_policy(sites=(s,), revision=2))
            hz.fixture.receipts.reset()
            assert (await hz.fetch("GET", good, headers=NAV))[0] == 403
            assert nothing_reached(hz)
    run(scenario)


async def _raw_connect(port, target):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return reader, writer, head


def test_no_tunnel_on_unknown_tls(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(h.open_web_policy())
            # Opaque bytes after CONNECT: never relayed.
            reader, writer, head = await _raw_connect(hz.browser_port, "site.example:443")
            writer.write(b"\x00\x01binary-not-tls-not-http" * 10)
            await writer.drain()
            try:
                await asyncio.wait_for(reader.read(100), 3)
            except (asyncio.TimeoutError, ConnectionError):
                pass
            writer.close()
            # TLS whose server name is another host than the CONNECT target: refused.
            reader, writer, head = await _raw_connect(hz.browser_port, "site.example:443")
            ctx = ssl.create_default_context(cafile=str(tmp / "ca" / "mitmproxy-ca-cert.pem"))
            ctx.check_hostname = False
            tr = await asyncio.get_running_loop().start_tls(writer.transport, writer.transport.get_protocol(),
                                                            ctx, server_hostname="other.example")
            tr.write(b"GET / HTTP/1.1\r\nHost: site.example\r\n\r\n")
            await asyncio.sleep(1)
            tr.close()
            await asyncio.sleep(0.2)
            assert nothing_reached(hz)
    run(scenario)


async def _raw_tls_request(hz, connect_target, sni, raw: bytes) -> bytes:
    """One raw HTTP/1.1 request inside a CONNECT tunnel, bytes exactly as given."""
    reader, writer, head = await _raw_connect(hz.browser_port, connect_target)
    ctx = ssl.create_default_context(cafile=str(hz.tmp / "ca" / "mitmproxy-ca-cert.pem"))
    ctx.check_hostname = False
    ctx.set_alpn_protocols(["http/1.1"])
    loop = asyncio.get_running_loop()
    tr = await loop.start_tls(writer.transport, writer.transport.get_protocol(), ctx, server_hostname=sni)
    reader._transport = tr
    writer._transport = tr
    tr.write(raw)
    try:
        return await asyncio.wait_for(reader.read(200), 5)
    except (asyncio.TimeoutError, ConnectionError):
        return b""
    finally:
        tr.close()


def test_host_port_that_does_not_parse_is_refused(tmp):
    """A Host port of Unicode digits (``str.isdigit`` true, ``int`` fails) — a request a page
    cannot make, but a compromised browser process could — is refused, never forwarded."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(account_policy())
            assert (await hz.fetch("GET", "https://site.example/account", headers=NAV))[0] == 200
            hz.fixture.receipts.reset()
            raw = ("POST /api/track HTTP/1.1\r\nHost: site.example:\u00b2\r\n"
                   "Content-Length: 11\r\n\r\nsecret=1234").encode("utf-8")
            resp = await _raw_tls_request(hz, "site.example:443", "site.example", raw)
            await asyncio.sleep(0.3)
            assert not resp.startswith(b"HTTP/1.1 200")
            assert hz.fixture.receipts.requests == []
    run(scenario)


def test_absolute_form_naming_another_host_is_refused(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(h.open_web_policy())
            raw = b"GET https://other.example/x HTTP/1.1\r\nHost: site.example\r\n\r\n"
            resp = await _raw_tls_request(hz, "site.example:443", "site.example", raw)
            await asyncio.sleep(0.3)
            assert not resp.startswith(b"HTTP/1.1 200")
            assert hz.fixture.receipts.requests == []
    run(scenario)


def test_a_hook_that_raises_refuses(tmp, monkeypatch):
    """mitmproxy carries on after an addon exception; the addon must not."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(h.open_web_policy())
            # A fresh pin for the host (an allowed request first), so only the hook's own
            # refusal stands between the raise and the upstream.
            assert (await hz.fetch("GET", "https://site.example/", headers=NAV))[0] == 200
            hz.fixture.receipts.reset()

            def boom(flow):
                raise ValueError("parse bug")
            monkeypatch.setattr(hz.addon, "_meta", boom)
            status, _ = await hz.fetch("POST", "https://site.example/a", data=b"x=1")
            assert status != 200
            assert nothing_reached(hz)
            assert hz.addon.events[-1].reason == "internal"
    run(scenario)


def test_an_oversized_body_is_not_read(tmp):
    """A declared body over the bound is refused at its headers and the rest is never read
    (the proxy closes the stream instead of buffering it)."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(h.open_web_policy())
            reader, writer, head = await _raw_connect(hz.browser_port, "site.example:443")
            ctx = ssl.create_default_context(cafile=str(hz.tmp / "ca" / "mitmproxy-ca-cert.pem"))
            ctx.set_alpn_protocols(["http/1.1"])
            loop = asyncio.get_running_loop()
            tr = await loop.start_tls(writer.transport, writer.transport.get_protocol(), ctx,
                                      server_hostname="site.example")
            reader._transport = tr
            tr.write(b"POST /up HTTP/1.1\r\nHost: site.example\r\nContent-Length: 50000000\r\n\r\n"
                     + b"x" * 4096)
            started = loop.time()
            try:
                data = await asyncio.wait_for(reader.read(100), 5)
            except (ConnectionError, OSError):
                data = b""
            assert loop.time() - started < 4  # answered or closed at once, not after 50 MB
            assert not data.startswith(b"HTTP/1.1 200")
            tr.close()
            assert nothing_reached(hz)
            assert hz.addon.events[-1].reason == "body_bound"
    run(scenario)


def test_request_body_bound(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(h.open_web_policy())
            status, _ = await hz.fetch("POST", "https://site.example/upload", data=b"x" * (1024 * 1024 + 1))
            assert status != 200 and nothing_reached(hz)  # refused and reset (413 + kill)
            assert hz.addon.events[-1].reason == "body_bound"
    run(scenario)


def test_broker_listener_fixed_hosts(tmp):
    async def scenario():
        resolver = h.fixed_resolver({"acct.1password.eu": [h.FIXTURE_IP], "unlisted.1password.eu": [h.FIXTURE_IP],
                                     "private.1password.eu": [h.PRIVATE_IP]})
        async with Harness(tmp, HOSTS + ["private.1password.eu"], resolver=resolver,
                           account_hosts=frozenset({"acct.1password.eu", "private.1password.eu"})) as hz:
            for url in ["https://unlisted.1password.eu/api/v1/vaults", "https://private.1password.eu/api/v1/vaults",
                        "http://acct.1password.eu/api/v1/vaults", "https://acct.1password.eu:8443/api"]:
                status, _ = await hz.fetch("GET", url, broker=True)
                assert status in (403, None), url
            # SNI naming another host than the CONNECT target.
            reader, writer, head = await _raw_connect(hz.broker_port, "acct.1password.eu:443")
            assert b"200" in head.split(b"\r\n")[0]
            ctx = ssl.create_default_context(cafile=str(tmp / "ca" / "mitmproxy-ca-cert.pem"))
            ctx.check_hostname = False
            tr = await asyncio.get_running_loop().start_tls(writer.transport, writer.transport.get_protocol(),
                                                            ctx, server_hostname="unlisted.1password.eu")
            tr.write(b"GET /api HTTP/1.1\r\nHost: acct.1password.eu\r\n\r\n")
            await asyncio.sleep(1)
            tr.close()
            await asyncio.sleep(0.2)
            assert nothing_reached(hz)
            status, _ = await hz.fetch("GET", "https://acct.1password.eu/api/v1/vaults", broker=True)
            assert status == 200
            assert hz.fixture.receipts.requests == [("GET", "acct.1password.eu", "/api/v1/vaults", b"")]
            # The broker listener applies no run policy, and the browser listener none of the
            # broker's: with no policy installed the browser cannot reach the account host.
            hz.fixture.receipts.reset()
            assert (await hz.fetch("GET", "https://acct.1password.eu/api/v1/vaults"))[0] == 403
            assert nothing_reached(hz)
    run(scenario)


def test_extension_host_passes_in_an_account_session(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS, account_hosts=frozenset({"acct.1password.eu"})) as hz:
            hz.addon.install(account_policy())
            assert (await hz.fetch("POST", "https://acct.1password.eu/api/v2/auth", data=b"{}"))[0] == 200
            assert (await hz.fetch("POST", "https://unlisted.1password.eu/api/v2/auth", data=b"{}"))[0] == 403
            assert len(hz.fixture.receipts.requests) == 1
    run(scenario)


def test_login_lease_closes_at_landing_document(tmp):
    """The first document response after ``arm_login_close`` ends the lease before the
    landing page reaches the browser; a later request to the login origin is decided without
    the lease (the site's own traffic, never ``login_lease``)."""
    lease = Lease("L1", "r1", "s1", 1, "login")

    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            client = ProxyClient(hz.control_path)
            await client.install(account_policy())
            await client.begin_login(lease)
            assert (await hz.fetch("POST", "https://login.example/u/login", headers=FETCH, data=b"user=x"))[0] == 200
            await client.arm_login_close("L1")
            assert (await hz.fetch("POST", "https://login.example/u/login/password?as=html",
                                   headers={**NAV, "content-type": "application/x-www-form-urlencoded"},
                                   data=b"password=x"))[0] == 200
            assert hz.addon.policy.login_lease is None
            assert (await hz.fetch("POST", "https://login.example/api/prefs", headers=FETCH, data=b"{}"))[0] == 200
            assert hz.addon.events[-1].reason == "site_page"
            events, _ = await client.events(0)
            lease_events = [e for e in events if e.reason == "login_lease"]
            assert lease_events and all(e.binding is None for e in lease_events)
            assert any(e.event_kind == "login_lease_closed" for e in events)
    run(scenario)


def test_control_socket_admits_only_the_controller(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.proxy.controller_uid = os.getuid() + 1
            with pytest.raises(ControlError):
                await ProxyClient(hz.control_path, timeout=3).install(h.open_web_policy())
            assert hz.addon.policy is None
    run(scenario)


def test_malformed_install_keeps_the_current_policy(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            client = ProxyClient(hz.control_path)
            await client.install(h.open_web_policy(revision=3))
            bad = encode_policy(account_policy())
            bad["approvals"] = [{"id": "x"}]
            reply = hz.proxy.dispatch(json.dumps({"op": "install", "policy": bad}).encode())
            assert reply == {"ok": False, "error": "unsupported"}
            assert hz.addon.policy.revision == 3
    run(scenario)


def test_policy_wire_round_trip():
    """Policy round-trips tags only and refuses each legacy raw-component member."""
    lease = Lease('L1', 'r1', 's1', 1, 'login')
    key = canary().encode()
    tagger = lambda label, raw: hmac.new(key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()
    binding = ProviderBinding('https://p.example', path_tag=tagger('path', b'/auth'),
                              client_id_tag=tagger('client_id', b'c'),
                              redirect_uri_tag=tagger('redirect_uri', b'https://l.example/cb'))
    p = account_policy(login_lease=lease,
                       sites=(site(bindings=(binding,)),))
    wire = json.loads(json.dumps(encode_policy(p)))
    expected = {'provider_origin': binding.provider_origin, 'path_tag': binding.path_tag,
                'client_id_tag': binding.client_id_tag, 'redirect_uri_tag': binding.redirect_uri_tag}
    # Also try the old format by itself: ignoring raw fields must not certify it.
    raw = {'authorization_path': '/auth', 'client_id': 'c', 'redirect_uri': 'https://l.example/cb'}
    for legacy in [{**expected, name: value} for name, value in raw.items()] + [
            {'provider_origin': binding.provider_origin, **raw}]:
        bad = json.loads(json.dumps(wire))
        bad['sites'][0]['provider_bindings'] = [legacy]
        with pytest.raises(ControlError, match='unsupported'):
            decode_policy(bad)
    assert wire['sites'][0]['provider_bindings'] == [expected]
    q = decode_policy(wire)
    assert q.manual_lease == p.manual_lease and q.login_lease == p.login_lease
    assert q.active_session == p.active_session and q.site_grants == p.site_grants
    assert [s.provider_bindings for s in q.sites] == [s.provider_bindings for s in p.sites]


def test_alt_svc_removed(tmp):
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            hz.addon.install(h.open_web_policy())
            import aiohttp as a
            conn = a.TCPConnector(ssl=hz.client_ctx, force_close=True)
            async with a.ClientSession(connector=conn) as s:
                async with s.get("https://site.example/", proxy=hz.proxy_url) as r:
                    assert "alt-svc" not in {k.lower() for k in r.headers}
    run(scenario)


# --------------------------------------------------------------------------- addon layers alone
# Each test below reaches one defence-in-depth layer with every outer layer absent (no proxy
# server attached, so no connection closing; no PinnedLoop), so that layer alone decides.

from mitmproxy.test import tflow, tutils  # noqa: E402

from casa_browser.proxy_addon import EnforcingAddon  # noqa: E402

BROWSER_PORT, BROKER_PORT = 3128, 3129


def _flow(method="GET", host="site.example", path="/", port=443, scheme="https", headers=None,
          content=b"", listener=BROWSER_PORT, sni="site.example"):
    req = tutils.treq(method=method.encode(), scheme=scheme.encode(), host=host, port=port,
                      path=path.encode(), content=content, authority=b"",
                      headers=__import__("mitmproxy").http.Headers(
                          host=host if port in (443, 80) else f"{host}:{port}", **(headers or {})))
    f = tflow.tflow(req=req)
    f.client_conn.sockname = ("127.0.0.1", listener)
    f.client_conn.sni = sni
    return f


def _addon(resolver=None, **kw):
    return EnforcingAddon(resolver=resolver or h.fixed_resolver({
        "site.example": [h.FIXTURE_IP], "acct.1password.eu": [h.FIXTURE_IP],
        "unlisted.1password.eu": [h.FIXTURE_IP]}), browser_port=BROWSER_PORT,
        broker_port=BROKER_PORT, **kw)


def test_decided_again_on_the_policy_current_after_resolution():
    a = None

    async def resolver(host, port):
        # a refusing policy arrives while the host resolves: an account session of another site
        a.install(account_policy(sites=(site(origins=("https://other.example",)),)))
        return [h.FIXTURE_IP]

    a = _addon(resolver)
    a.install(h.open_web_policy())
    f = _flow()
    asyncio.run(a.request(f))
    assert f.response is not None and f.response.status_code == 403
    assert a.events[-1].reason != "open_web"


def test_websocket_frame_dropped_when_the_policy_changed():
    a = _addon()
    a.install(h.open_web_policy())
    f = tflow.twebsocketflow()
    f.client_conn.sockname = ("127.0.0.1", BROWSER_PORT)
    from casa_browser.policy import RequestMeta
    from casa_browser.proxy_addon import _Socket
    a.sockets[f.id] = _Socket(
        f, RequestMeta("GET", "https://site.example/ws", resource_type="websocket"), a.epoch)
    a.websocket_message(f)
    assert not f.websocket.messages[-1].dropped
    # a change no recheck has seen yet, to a policy that refuses the socket (another run's
    # session: session_mismatch)
    a.policy = dataclasses.replace(account_policy(), active_session=dataclasses.replace(
        account_policy().active_session, site_id="gone"))
    a.epoch += 1
    a.websocket_message(f)
    assert f.websocket.messages[-1].dropped and f.id not in a.sockets
    g = tflow.twebsocketflow()  # a socket the addon never admitted
    a.websocket_message(g)
    assert g.websocket.messages[-1].dropped


def test_no_connection_without_a_fresh_pin():
    from types import SimpleNamespace
    a = _addon()
    data = SimpleNamespace(server=SimpleNamespace(address=("site.example", 443), error=None))
    a.server_connect(data)
    assert data.server.error
    for literal in (ip4(10, 0, 0, 1), "127.0.0.1", "[::1]"):
        data = SimpleNamespace(server=SimpleNamespace(address=(literal, 443), error=None))
        a.server_connect(data)
        assert data.server.error, literal
    asyncio.run(a._target("site.example", 443))
    data = SimpleNamespace(server=SimpleNamespace(address=("site.example", 443), error=None))
    a.server_connect(data)
    assert data.server.error is None


def test_connect_to_a_private_literal_refused_at_connect():
    a = _addon()
    a.install(h.open_web_policy())
    for host in (ip4(192, 168, 1, 20), "127.0.0.1", "[fe80::1]", "homeassistant", "nas.local"):
        f = _flow(method="CONNECT", host=host, path="", sni=None)
        a.http_connect(f)
        assert f.response is not None and f.response.status_code == 403, host


def test_broker_request_needs_a_listed_host():
    a = _addon(broker_hosts=frozenset({"acct.1password.eu"}))
    ok = _flow(host="acct.1password.eu", path="/api/v1/vaults", listener=BROKER_PORT,
               sni="acct.1password.eu")
    asyncio.run(a.request(ok))
    assert ok.response is None
    bad = _flow(host="unlisted.1password.eu", path="/api/v1/vaults", listener=BROKER_PORT,
                sni="unlisted.1password.eu")
    asyncio.run(a.request(bad))
    assert bad.response is not None and bad.response.status_code == 403


# --------------------------------------------------------------------------- Chrome update hosts

from casa_browser.policy import RequestMeta, decide  # noqa: E402

CRX_Q = ("os=linux&arch=x86_64&prod=chromecrx&prodchannel=&prodversion=155.0.8059.84&lang=en-GB"
         "&acceptformat=crx3,puff&x=id%3Daeblfdkhhhdcdjpifhhbdiojplfjncoa%26installsource%3Dpolicy%26uc")
CUP_Q = "cup2key=15:AbCdEf0123456789_-&cup2hreq=" + "0123456789abcdef" * 4


@pytest.mark.parametrize("method,url,body,ct", [
    ("GET", "https://clients2.google.com/service/update2/crx?" + CRX_Q, b"", None),
    ("GET", "https://clients2.googleusercontent.com/crx/blobs/AbC-123_x/abc.crx", b"", None),
    ("POST", "https://update.googleapis.com/service/update2/json?" + CUP_Q, b"{}", "application/json"),
])
def test_chrome_update_hosts_pass_in_an_account_session(method, url, body, ct):
    meta = RequestMeta(method, url, body, ct, {"content-type": ct} if ct else {})
    assert decide(meta, account_policy()).reason == "infrastructure"


@pytest.mark.parametrize("method,url,body,ct", [
    ("POST", "https://clients2.google.com/service/update2/crx?" + CRX_Q, b"x", "text/plain"),
    ("GET", "https://clients2.google.com/service/update2/other?" + CRX_Q, b"", None),
    ("GET", "https://clients2.google.com/service/update2/crx?q=<x>", b"", None),
    ("GET", "https://www.google.com/service/update2/crx?" + CRX_Q, b"", None),
    ("POST", "https://update.googleapis.com/service/update2/json", b"{}", "application/json"),
    ("POST", "https://update.googleapis.com/service/update2/json?cup2key=1:a&cup2hreq=zz", b"{}", "application/json"),
    ("POST", "http://update.googleapis.com/service/update2/json?" + CUP_Q, b"{}", "application/json"),
    ("GET", "https://redirector.gvt1.com/edgedl/chromewebstore/x/y.crx", b"", None),
    ("GET", "https://clients2.googleusercontent.com/crx/blobs/../x", b"", None),
])
def test_chrome_update_hosts_refused_outside_their_exact_shape(method, url, body, ct):
    meta = RequestMeta(method, url, body, ct, {"content-type": ct} if ct else {})
    assert not decide(meta, account_policy()).allow


def test_chrome_update_hosts_not_infrastructure_outside_an_account_session():
    import dataclasses
    from casa_browser.policy import SessionRef
    from casa_browser.protocol import SiteGrant
    meta = RequestMeta("GET", "https://clients2.google.com/service/update2/crx?" + CRX_Q)
    public = dataclasses.replace(account_policy(grants=(SiteGrant("s1", "public"),)),
                                 active_session=SessionRef("public", None, 1))
    assert decide(meta, public).reason != "infrastructure"


@pytest.mark.parametrize("version,method,headers,refused", [
    ("HTTP/2.0", "POST", {}, True),                       # a streamed upload: no declared size
    ("HTTP/2.0", "POST", {"content-length": "3"}, False),
    ("HTTP/2.0", "GET", {}, False),
    ("HTTP/2.0", "HEAD", {}, False),
    ("HTTP/2.0", "OPTIONS", {"access-control-request-method": "PUT"}, False),  # a preflight
    ("HTTP/2.0", "OPTIONS", {}, True),
    ("HTTP/2.0", "DELETE", {}, True),                     # fetch may stream a body on these
    ("HTTP/2.0", "PATCH", {}, True),
    ("HTTP/2.0", "DELETE", {"content-length": "0"}, False),
    ("HTTP/1.1", "POST", {"transfer-encoding": "chunked"}, True),
    ("HTTP/1.1", "POST", {"content-length": "1", "transfer-encoding": "chunked"}, True),
    ("HTTP/1.1", "POST", {"content-length": "1048577"}, True),
    ("HTTP/1.1", "POST", {"content-length": "²"}, True),
])
def test_unbounded_bodies_refused_at_their_headers(version, method, headers, refused):
    a = _addon()
    f = _flow(method=method, headers=headers)
    f.request.http_version = version
    for k in [k for k in f.request.headers if k.lower() == "content-length" and "content-length" not in headers]:
        del f.request.headers[k]
    a.requestheaders(f)
    assert (f.response is not None) is refused
    if refused:
        assert f.killable is False  # killed: mitmproxy stops reading the body


def test_a_memory_error_mitmproxy_catches_ends_the_process(monkeypatch):
    """mitmproxy catches a connection handler's exception and logs it; at the memory limit the
    proxy must exit (s6 restarts it holding no policy) instead of running on wedged."""
    import logging
    import sys
    from casa_browser import proxy_main
    exits = []
    monkeypatch.setattr(proxy_main.os, "_exit", lambda code: exits.append(code))
    try:
        raise MemoryError()
    except MemoryError:
        info = sys.exc_info()
    record = logging.LogRecord("mitmproxy.proxy.server", logging.ERROR, __file__, 1,
                               "connection handler has crashed: %s", ("x",), info)
    proxy_main._Withhold().filter(record)
    assert exits == [70]
    ordinary = logging.LogRecord("mitmproxy.proxy.server", logging.ERROR, __file__, 1, "x", (), None)
    proxy_main._Withhold().filter(ordinary)
    assert exits == [70]


@pytest.mark.parametrize("length", ["0", "1", "2097152", "2097153", None])
def test_every_response_streams_unbuffered(length):
    """No response body is held in the proxy, whatever its declared size: a buffered one stays
    in memory, several times over, until garbage collection reaches its flow."""
    f = _flow()
    f.response = tutils.tresp(content=b"")
    f.response.headers.pop("content-length", None)
    if length is not None:
        f.response.headers["content-length"] = length
    _addon().responseheaders(f)
    assert f.response.stream is True


def test_the_event_ring_is_bounded_by_bytes_and_counts_what_it_drops():
    """The ring is bounded by its total size as well as by count: the oldest events go first,
    and every one that goes is counted."""
    from casa_browser import proxy_addon as pa
    from casa_browser.policy import Decision
    clock = [1000.0]
    a = _addon(clock=lambda: clock[0])
    # Outside a credential window (a started proxy opens one; close it as the controller does).
    a.close_window(a.token)
    clock[0] += 4
    assert not a.window_open
    big = "https://" + "x" * (2 * 1024 * 1024) + ".example"
    n = 3 * pa.EVENT_BYTES // len(big)
    for _ in range(n):
        a._record("browser", Decision(False, "test", "request_refused"), "POST", big)
    assert a.event_bytes <= pa.EVENT_BYTES
    assert sum(pa._event_size(e) for e in a.events) == a.event_bytes
    assert a.dropped == n - len(a.events) and a.dropped > 0
    seqs = [e.seq for e in a.events]
    assert seqs == list(range(n - len(seqs) + 1, n + 1))   # the newest kept, in order
    for _ in range(pa.EVENT_RING + 5):
        a._record("browser", Decision(False, "test", "request_refused"), "GET")
    assert len(a.events) == pa.EVENT_RING and a.dropped == n + 5


def test_finished_request_bodies_do_not_wait_for_a_full_collection(tmp, monkeypatch):
    """Finished flows hold their request bodies in reference cycles; the proxy's timed full
    collection frees them, so repeated uploads keep no bodies alive between rounds."""
    import gc
    import tracemalloc
    from casa_browser import proxy_main
    monkeypatch.setattr(proxy_main, "GC_PERIOD", 0.2)
    monkeypatch.setattr(proxy_main.Proxy._collect_garbage, "__defaults__", (0.2,))
    from casa_browser import proxy_addon
    monkeypatch.setattr(proxy_addon, "EVENT_BYTES", 1_000_000)   # keep the ring out of the count
    gc.collect()
    gc.disable()   # only the proxy's own collector runs
    try:
        async def scenario():
            async with Harness(tmp, HOSTS) as hz:
                hz.addon.install(h.open_web_policy())
                import aiohttp as a
                body = b"x" * 1_000_000
                conn = a.TCPConnector(ssl=hz.client_ctx, force_close=True, limit=0)
                after = []
                async with a.ClientSession(connector=conn) as s:
                    async def one():
                        async with s.post("https://site.example/up", data=body, proxy=hz.proxy_url,
                                          headers={"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                                                   "Content-Type": "application/octet-stream"}) as r:
                            await r.read()
                            return r.status
                    tracemalloc.start()
                    try:
                        for _ in range(3):
                            assert set(await asyncio.gather(*(one() for _ in range(20)))) == {200}
                            hz.fixture.receipts.requests.clear()
                            await asyncio.sleep(0.6)
                            after.append(tracemalloc.get_traced_memory()[0])
                    finally:
                        tracemalloc.stop()
                # Uncollected, each round would leave its 20 MB of bodies behind.
                assert after[2] - after[0] < 10_000_000, after
        run(scenario)
    finally:
        gc.enable()
        gc.unfreeze()


def test_an_open_websocket_keeps_no_message_history(tmp):
    """A long-lived allowed WebSocket relays every message and keeps none of them: only the
    frame being decided stays on its flow."""
    async def scenario():
        async with Harness(tmp, HOSTS) as hz:
            wss = "wss://site.example/live"
            await ProxyClient(hz.control_path).install(account_policy())
            conn = aiohttp.TCPConnector(ssl=hz.client_ctx)
            async with aiohttp.ClientSession(connector=conn) as s:
                ws = await s.ws_connect(wss, proxy=hz.proxy_url,
                                        timeout=aiohttp.ClientWSTimeout(ws_receive=5))
                chunk = "m" * 100_000
                for i in range(50):
                    await ws.send_str(f"{i}:{chunk}")
                    assert (await ws.receive(timeout=5)).data == f"echo:{i}:{chunk}"
                (sock,) = hz.addon.sockets.values()
                assert len(sock.flow.websocket.messages) <= 1
                assert len(hz.fixture.receipts.ws_messages) == 50
                await ws.close()
    run(scenario)


def test_the_pin_table_is_bounded():
    """Endless distinct hosts never grow the pin table: expired pins go first, then the oldest;
    the newest pin is always kept."""
    from casa_browser.netfloor import ResolvedTarget
    from casa_browser.proxy_addon import Pins
    import ipaddress
    now = [0.0]
    pins = Pins(clock=lambda: now[0])
    addr = (ipaddress.ip_address(h.FIXTURE_IP),)
    for i in range(3 * Pins.MAX):
        now[0] = i * 0.001
        pins.put(ResolvedTarget(f"h{i}.example", 443, addr))
    assert len(pins._pins) <= Pins.MAX
    assert pins.fresh(f"h{3 * Pins.MAX - 1}.example", 443) == addr


# Page-free credential-window tests drive the production hooks directly, without TCP.
import base64
import hashlib
import hmac
from dataclasses import replace
from brokerfakes import Clock, canary
from casa_browser.proxy_client import encode_event, decode_event


def _credential_addon(clock=None, **kwargs):
    async def resolver(host, port):
        return [h.FIXTURE_IP]
    a = _addon(resolver, clock=clock or Clock(), extension_hosts=frozenset({'acct.1password.eu'}), **kwargs)
    a.install(account_policy())
    return a


async def _record_flow(a, flow):
    a.requestheaders(flow)
    await a.request(flow)
    assert a.events
    return a.events[-1]


def _vocabulary(event, value=None, origin='https://login.example'):
    encoded = encode_event(event)
    assert event.origin == origin
    assert decode_event(encoded) == event
    if value:
        variants = [value, base64.urlsafe_b64encode(value.encode()).decode(),
                    ''.join('%%%02X' % b for b in value.encode())]
        for variant in variants:
            assert variant.encode() not in json.dumps(encoded).encode()
    assert set(encoded) == {'seq', 'run_id', 'revision', 'listener', 'at', 'allow', 'reason',
                            'event_kind', 'method', 'resource_type', 'is_navigation', 'origin',
                            'binding'}


async def _credential_post(hz, value, *, allowed=False):
    """Pin each send to relay receipts as well as its recorded decision. An allowed one is the
    login origin's own data request; a refused one is a top-level document POST off the site
    (design §4: ``site_lock``), whose origin the window never names."""
    hz.fixture.receipts.reset()
    url, headers = (('https://login.example/again', FETCH) if allowed
                    else ('https://outside.example/again', NAV))
    status, _ = await hz.fetch('POST', url, headers=headers, data=value.encode())
    if allowed:
        assert status == 200
        assert hz.fixture.receipts.connections > 0 and hz.fixture.receipts.bytes > 0
        assert hz.fixture.receipts.requests == [('POST', 'login.example', '/again', value.encode())]
    else:
        assert status == 403
        assert nothing_reached(hz)
    event = hz.addon.events[-1]
    assert event.method == 'POST' and event.allow is allowed
    return event


def test_window_outlives_m5_closure(tmp_path):
    """A real 307 closes authority; its credential resend delivers zero upstream receipts."""
    async def scenario():
        async with Harness(tmp_path, HOSTS) as hz:
            client = ProxyClient(hz.control_path)
            await client.install(account_policy())
            await client.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
            await client.arm_login_close('l')
            hz.fixture.pages['/redirect'] = ('text/html', b'', {'Location': '/again'})
            # The fixture returns an actual redirect status through its test-only page table.
            hz.fixture.status['/redirect'] = 307
            value = canary()
            status, _ = await hz.fetch('POST', 'https://login.example/redirect', headers=NAV, data=value.encode())
            assert status == 307
            assert hz.fixture.receipts.requests == [('POST', 'login.example', '/redirect', value.encode())]
            assert hz.fixture.receipts.connections > 0 and hz.fixture.receipts.bytes > 0
            assert hz.addon.policy.login_lease is None
            _vocabulary(await _credential_post(hz, value), value, origin=None)
    run(scenario)


def _provider_site(hz):
    """A site whose sign-in goes through a provider, and the provider page only a login lease
    admits (``test_provider_binding_exact``)."""
    key = hz.addon.binding_key_path.read_bytes()
    tagger = lambda label, raw: hmac.new(key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()
    binding = ProviderBinding('https://provider.example',
                              path_tag=tagger('path', b'/o/oauth2/auth'),
                              client_id_tag=tagger('client_id', b'client-123'),
                              redirect_uri_tag=tagger('redirect_uri', b'https://login.example/cb'))
    return site(bindings=(binding,)), ("https://provider.example/o/oauth2/auth?client_id="
                                       "client-123&redirect_uri=https://login.example/cb")


@pytest.mark.parametrize('kind', ['login', 'manual'])
def test_killed_request_resend_stays_in_window(tmp_path, kind):
    """Resends after authority withdrawal stay withheld and never reach upstream."""
    async def scenario():
        clock = Clock()
        async with Harness(tmp_path, HOSTS, clock=clock,
                           binding_key_path=tmp_path / 'proxy' / 'binding.key') as hz:
            a = hz.addon
            provider, page = _provider_site(hz)
            a.install(account_policy(sites=(provider,)))
            lease = Lease('l', 'r1', 's1', 1, kind)
            token = a.begin_login(lease) if kind == 'login' else a.begin_manual(lease, purpose='login')
            if kind == 'login':
                # slice 5r: the end of a login lease withdraws authority only after it admitted
                # what the policy without it refuses (here the provider's sign-in page)
                assert (await hz.fetch('GET', page, headers=NAV))[0] == 200
                hz.fixture.request_seen.clear()
                hz.fixture.receipts.reset()
            value = canary()
            # Hold the admitted response so withdrawal kills a real in-flight POST.
            gate = hz.fixture.response_hold = asyncio.Event()
            pending = asyncio.create_task(hz.fetch('POST', 'https://login.example/again',
                                                   headers=FETCH, data=value.encode()))
            try:
                await asyncio.wait_for(hz.fixture.request_seen.wait(), 3)
                assert hz.fixture.receipts.connections > 0 and hz.fixture.receipts.bytes > 0
                assert hz.fixture.receipts.requests == [('POST', 'login.example', '/again', value.encode())]
                if kind == 'login':
                    a.end_login('l')
                else:
                    a.end_manual('l')
                assert (await asyncio.wait_for(pending, 3))[0] != 200
            finally:
                gate.set()
                hz.fixture.response_hold = None
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            a.close_window(token)
            _vocabulary(await _credential_post(hz, value), value, origin=None)
            clock.advance(2)
            a.install(account_policy(sites=(provider,)))
            clock.advance(1.01)
            _vocabulary(await _credential_post(hz, value), value, origin=None)
            clock.advance(2.01)
            raw = await _credential_post(hz, value)
            # Outside the window the origin is recorded again.
            assert raw.origin == 'https://outside.example'
    run(scenario)


def test_opener_cancels_pending_close(tmp_path):
    """A new opener cancels an earlier deferred close beyond its old deadline."""
    async def scenario():
        clock = Clock()
        async with Harness(tmp_path, HOSTS, clock=clock) as hz:
            a = hz.addon
            a.install(account_policy())
            token = a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
            a.end_login('l')
            a.close_window(token)
            clock.advance(1)
            a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='login')
            clock.advance(4)
            value = canary()
            _vocabulary(await _credential_post(hz, value, allowed=True), value)
    run(scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize('arrival_open', [True, False])
async def test_window_fixed_at_arrival(arrival_open):
    """A held resolution is withheld if the window is open at either arrival or recording."""
    clock = Clock()
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolver(host, port):
        entered.set()
        await release.wait()
        return [h.FIXTURE_IP]
    a = _addon(resolver, clock=clock)
    a.install(h.open_web_policy())
    token = a.open_window()
    if not arrival_open:
        a.close_window(token)
        clock.advance(4)
    value = canary()
    flow = _flow(path='/' + value)
    a.requestheaders(flow)
    task = asyncio.create_task(a.request(flow))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if arrival_open:
            a.close_window(token)
            clock.advance(4)
        else:
            a.open_window()
        release.set()
        await task
        _vocabulary(a.events[-1], value, origin=None)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_delayed_hook_exception_keeps_arrival_window(monkeypatch):
    """A hook failure after window closure still withholds its arriving request's method."""
    a = _credential_addon()
    value = canary()
    flow = _flow(method=value)
    a.requestheaders(flow)
    assert flow.response is None
    a.close_window(a.token)
    a.clock.advance(4)
    assert not a.window_open

    def boom(flow):
        raise ValueError('parse bug')

    monkeypatch.setattr(a, '_meta', boom)
    await a.request(flow)

    assert flow.response is not None and flow.response.status_code == 403
    assert len(a.events) == 1
    event = a.events[-1]
    assert not event.allow and event.reason == 'internal'
    assert event.method == 'OTHER'
    _vocabulary(event, value, origin=None)


def test_stale_close_ignored(tmp_path):
    """An older opener or proxy boot cannot close the current credential window."""
    async def scenario():
        clock = Clock()
        old = current = None
        for restart in range(2):
            async with Harness(tmp_path, HOSTS, clock=clock) as hz:
                a = hz.addon
                a.install(account_policy())
                if not restart:
                    old, current = a.open_window(), a.open_window()
                    assert old != current
                    a.close_window(old)
                else:
                    fresh = a.open_window()
                    assert fresh != old and fresh != current
                    a.close_window(current)
                clock.advance(4)
                value = canary()
                _vocabulary(await _credential_post(hz, value), value, origin=None)
    run(scenario)


@pytest.mark.parametrize('change', ['start', 'end_login', 'end_manual', 'install', 'revoke'])
def test_window_closes_only_at_close_window(change, tmp_path):
    """Authority changes preserve withholding; every denied send has zero receipts."""
    async def scenario():
        clock = Clock()
        async with Harness(tmp_path, HOSTS, clock=clock) as hz:
            a = hz.addon
            a.install(account_policy())
            if change == 'start':
                status = await ProxyClient(hz.control_path).status()
                assert status.get('window_open') is True and status.get('token') is not None
                token = status['token']
            else:
                token = a.open_window()
            if change == 'end_login':
                token = a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
                a.end_login('l')
            elif change == 'end_manual':
                token = a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='assist')
                a.end_manual('m')
            elif change == 'install':
                a.install(account_policy())
            elif change == 'revoke':
                a.revoke('r1')
            clock.advance(20)
            value = canary()
            _vocabulary(await _credential_post(hz, value), value, origin=None)
            if change == 'start':
                await ProxyClient(hz.control_path).close_window(token)
            else:
                a.close_window(token)
            clock.advance(4)
            event = await _credential_post(hz, value)
            # Outside the window the origin is recorded again.
            assert event.origin == 'https://outside.example'
    run(scenario)


@pytest.mark.parametrize('listener', ['browser', 'broker'])
@pytest.mark.parametrize('purpose', ['lease', 'login', 'assist'])
@pytest.mark.parametrize('allowed', [True, False])
@pytest.mark.parametrize('component', ['host', 'path', 'query', 'percent', 'base64', 'body', 'header', 'all'])
def test_credential_window_vocabulary(tmp_path, listener, purpose, allowed, component):
    """Both real listeners withhold request values; refusals deliver zero upstream bytes."""
    async def scenario():
        value = 'v' + canary().lower().replace('_', 'a').replace('-', 'b')
        host = ('acct.1password.eu' if listener == 'broker' else 'login.example') if allowed else 'outside.example'
        # A refused browser send is a top-level document off the site (design §4: site_lock).
        path, body = '/', b''
        headers = dict(NAV if (listener == 'browser' and not allowed) else FETCH)
        if component == 'host':
            host = value + '.' + host
        if component in ('path', 'all'):
            path += value
        if component in ('query', 'percent', 'base64', 'all'):
            encoded = (''.join('%%%02X' % byte for byte in value.encode()) if component == 'percent'
                       else base64.urlsafe_b64encode(value.encode()).decode() if component == 'base64' else value)
            path += '?value=' + encoded
        if component in ('body', 'all'):
            body = value.encode()
        if component in ('header', 'all'):
            headers['x-http-method-override'] = value
        listed = frozenset({host}) if listener == 'broker' and allowed else frozenset({'acct.1password.eu'})
        async with Harness(tmp_path, list(dict.fromkeys([*HOSTS, host])), account_hosts=listed) as hz:
            # Browser allowed host canaries become explicitly registered vocabulary.
            policy = account_policy(sites=(site(login=('https://' + host,)),)) if allowed and listener == 'browser' else account_policy()
            hz.addon.install(policy)
            if purpose == 'lease':
                hz.addon.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
            else:
                hz.addon.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose=purpose)
            status, _ = await hz.fetch('POST', 'https://' + host + path, headers=headers, data=body,
                                       broker=listener == 'broker')
            receipts = hz.fixture.receipts
            if allowed:
                assert status == 200
                assert receipts.connections > 0 and receipts.bytes > 0
                assert receipts.requests == [('POST', host, path, body)]
            else:
                assert status in (403, None)
                assert receipts.connections == 0
                assert receipts.bytes == 0
                assert receipts.requests == []
            events = hz.addon.events
            assert events
            event = events[-1]
            assert event.listener == listener and event.allow is allowed
            # Only registered/approved run origins may be exported, even on the broker listener.
            origin = 'https://' + host if allowed and listener == 'browser' else None
            _vocabulary(event, None if component == 'host' and origin else value, origin=origin)
    run(scenario)


def test_unknown_method_is_other_in_window(tmp_path):
    """A refused attacker-controlled method has no receipts or vocabulary escape."""
    async def scenario():
        async with Harness(tmp_path, HOSTS) as hz:
            hz.addon.install(account_policy())
            value = canary()
            # off the site, with no stated class: held to the document rule (site_lock)
            status, _ = await hz.fetch(value, 'https://outside.example/' + value)
            assert status == 403 and nothing_reached(hz)
            event = hz.addon.events[-1]
            assert not event.allow and event.method == 'OTHER'
            _vocabulary(event, value, origin=None)
    run(scenario)


def test_a_manual_lease_is_for_a_login_or_an_assist_view_only():
    """The demo live view is gone (action model, slice 7): a manual lease of any other purpose
    is refused before anything changes."""
    a = _credential_addon()
    before = a.policy
    with pytest.raises(ValueError, match='unsupported'):
        a.begin_manual(Lease('d', 'r1', 's1', 1, 'manual'), purpose='demo')
    assert a.policy is before and a.manual_purpose is None


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose', ['login', 'assist'])
async def test_binding_projection_and_callback(tmp_path, purpose):
    """Only login live views project keyed raw-component tags and callback references."""
    a = _credential_addon(binding_key_path=tmp_path / 'proxy' / 'binding.key')
    a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose=purpose)
    value = canary()
    path = '/auth/' + value
    callback = 'https%3A%2F%2Fsite.example%2Fcallback'
    flow = _flow(host='login.example', sni='login.example', headers=NAV,
                 path=path + '?client_id=' + value + '&redirect_uri=' + callback)
    event = await _record_flow(a, flow)
    _vocabulary(event, value)
    if purpose == 'assist':
        assert event.binding is None
        return
    key = a.binding_key_path.read_bytes()
    expected = {label + '_tag': hmac.new(key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()
                for label, raw in [('path', path.encode()), ('client_id', value.encode()), ('redirect_uri', callback.encode())]}
    assert event.binding == {'candidate': 1, **expected}
    callback_event = await _record_flow(a, _flow(path='/callback', headers=NAV))
    assert callback_event.binding == {'callback_of': 1}


def test_begin_manual_requires_purpose(tmp_path):
    """The proxy control socket refuses a manual opener lacking purpose."""
    from casa_browser.proxy_main import Proxy
    from casa_browser.proxy_client import _lease
    a = _credential_addon()
    p = Proxy(addon=a, confdir=tmp_path, control_path=tmp_path / 'control', controller_uid=os.getuid())
    reply = p.dispatch(json.dumps({'op': 'begin_manual', 'lease': _lease(Lease('m', 'r1', 's1', 1, 'manual'))}).encode())
    assert reply == {'ok': False, 'error': 'unsupported'}
    assert a.policy.manual_lease is None


def test_guest_sign_in_opens_window(tmp_path):
    """The explicitly opened window withholds an admitted guest credential POST."""
    async def scenario():
        async with Harness(tmp_path, HOSTS, account_hosts=frozenset({'acct.1password.eu'})) as hz:
            hz.addon.install(account_policy(sites=(site(login=('https://login.example',
                                                               'https://acct.1password.eu')),)))
            token = await ProxyClient(hz.control_path).open_window()
            assert token is not None
            values = [canary() for _ in range(3)]
            body = '&'.join(values).encode()
            status, _ = await hz.fetch('POST', 'https://acct.1password.eu/signin', data=body, broker=True)
            assert status == 200
            assert hz.fixture.receipts.connections > 0 and hz.fixture.receipts.bytes > 0
            assert hz.fixture.receipts.requests == [('POST', 'acct.1password.eu', '/signin', body)]
            event = hz.addon.events[-1]
            assert event.allow
            for value in values:
                _vocabulary(event, value, origin='https://acct.1password.eu')
    run(scenario)


@pytest.mark.asyncio
async def test_binding_key_persists_and_restore_invalidates_tags(tmp_path):
    """A proxy restart reuses its private key; removing it produces different binding tags."""
    import stat
    path = tmp_path / 'private' / 'binding.key'
    tags, key_ids = [], []
    for restart in range(3):
        if restart == 2:
            path.unlink()
        a = _addon(binding_key_path=path)
        key_ids.append(a.key_id)
        assert re.fullmatch(r'[0-9a-f]{16}', a.key_id) and a.key_id not in path.read_bytes().hex()
        a.install(account_policy())
        a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='login')
        event = await _record_flow(a, _flow(host='login.example', sni='login.example', headers=NAV,
                                           path='/auth?client_id=client&redirect_uri=https%3A%2F%2Fsite.example%2Fcallback'))
        tags.append(event.binding)
        assert len(path.read_bytes()) == 32
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert tags[0] == tags[1] and tags[1] != tags[2]
    # the key's id follows the key: the controller tells a restored (stale) binding by it
    assert key_ids[0] == key_ids[1] and key_ids[1] != key_ids[2]


@pytest.mark.parametrize('invalid', ['short', 'uppercase', 'nonhex'])
def test_provider_binding_tags_validated_on_policy_wire(invalid):
    """A policy install accepts only three lowercase 64-hex binding tags."""
    tag = hashlib.sha256(canary().encode()).hexdigest()
    binding = ProviderBinding('https://login.example', path_tag=tag, client_id_tag=tag, redirect_uri_tag=tag)
    policy = account_policy(sites=(site(bindings=(binding,)),))
    wire = encode_policy(policy)
    expected = {'provider_origin': 'https://login.example', 'path_tag': tag,
                'client_id_tag': tag, 'redirect_uri_tag': tag}
    assert wire['sites'][0]['provider_bindings'] == [expected]
    assert decode_policy(wire).sites[0].provider_bindings == (binding,)
    value = tag[:-1] if invalid == 'short' else tag.upper() if invalid == 'uppercase' else 'g' * 64
    wire['sites'][0]['provider_bindings'][0]['path_tag'] = value
    with pytest.raises(ControlError, match='unsupported'):
        decode_policy(wire)


@pytest.mark.asyncio
async def test_binding_candidate_with_unusable_callback():
    """A malformed callback still counts as a candidate, without a possible callback match."""
    a = _credential_addon()
    a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='login')
    event = await _record_flow(a, _flow(host='login.example', sni='login.example', headers=NAV,
                                      path='/auth?client_id=client&redirect_uri=relative'))
    assert event.binding == {'candidate': 1, 'path_tag': a.tagger('path', b'/auth'),
                             'client_id_tag': a.tagger('client_id', b'client'),
                             'redirect_uri_tag': a.tagger('redirect_uri', b'relative')}
    callback = await _record_flow(a, _flow(headers=NAV))
    assert callback.binding is None


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['client%5Fid=client&redirect%5Furi=https://site.example/callback',
                                  'CLIENT_ID=client&redirect_uri=https://site.example/callback',
                                  'client_id=client&redirect_uri=https://site.example/callback&CLIENT_ID=extra'])
async def test_binding_candidate_decoded_names(query):
    """Candidate names decode once, require lowercase spelling, and exclude case variants."""
    a = _credential_addon()
    a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='login')
    event = await _record_flow(a, _flow(host='login.example', sni='login.example', headers=NAV,
                                      path='/auth?' + query))
    if query.startswith('client%5Fid'):
        assert event.binding['candidate'] == 1
    else:
        assert event.binding is None


@pytest.mark.asyncio
async def test_binding_candidates_end_with_manual_lease():
    """Candidate references and numbering belong to one manual lease."""
    a = _credential_addon()
    for number in range(2):
        a.begin_manual(Lease(str(number), 'r1', 's1', 1, 'manual'), purpose='login')
        callback = await _record_flow(a, _flow(path='/callback', headers=NAV))
        assert callback.binding is None
        candidate = await _record_flow(a, _flow(host='login.example', sni='login.example', headers=NAV,
            path='/auth?client_id=client&redirect_uri=https://site.example/callback'))
        assert candidate.binding['candidate'] == 1
        a.end_manual(str(number))


def test_repeated_window_close_keeps_deadline():
    """A repeated current-token close cannot postpone the first close."""
    a = _credential_addon()
    token = a.open_window()
    a.close_window(token)
    a.clock.advance(2)
    a.close_window(token)
    a.clock.advance(1.01)
    assert not a.window_open


def test_m5_pushes_scheduled_window_close():
    """A post-submit authority closure extends an already scheduled quiet interval."""
    a = _credential_addon()
    token = a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
    a.arm_login_close('l')
    a.close_window(token)
    a.clock.advance(2)
    flow = _flow(host='login.example', sni='login.example', headers=NAV)
    flow.request.timestamp_start = a.clock()
    from mitmproxy import http
    flow.response = http.Response.make(200, b'')
    a.responseheaders(flow)
    assert a.policy.login_lease is None
    a.clock.advance(1.01)
    assert a.window_open
    a.clock.advance(2)
    assert not a.window_open


@pytest.mark.parametrize('failure', ['unreadable', 'invalid_size'])
def test_binding_key_replaced_when_unusable(tmp_path, monkeypatch, failure):
    """An unusable key is replaced before the proxy can issue binding tags."""
    from pathlib import Path
    path = tmp_path / 'proxy' / 'binding.key'
    first = _credential_addon(binding_key_path=path)
    old = first.tagger('path', b'/auth')
    if failure == 'unreadable':
        read_bytes = Path.read_bytes

        def unreadable(target):
            if target == path:
                raise PermissionError()
            return read_bytes(target)

        with monkeypatch.context() as patch:
            patch.setattr(Path, 'read_bytes', unreadable)
            second = _credential_addon(binding_key_path=path)
    else:
        path.write_bytes(b'')
        second = _credential_addon(binding_key_path=path)
    assert second.tagger('path', b'/auth') != old
    assert len(path.read_bytes()) == 32
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_binding_projection_survives_same_lease_install():
    """An updated policy carrying the same lease preserves its purpose and callback state."""
    a = _credential_addon()
    a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='login')
    event = await _record_flow(a, _flow(host='login.example', sni='login.example', headers=NAV,
        path='/auth?client_id=client&redirect_uri=https://site.example/callback'))
    assert event.binding['candidate'] == 1
    a.install(replace(a.policy, revision=2))
    callback = await _record_flow(a, _flow(headers=NAV, path='/callback'))
    assert callback.binding == {'callback_of': 1}


@pytest.mark.asyncio
async def test_authorization_candidate_precedes_callback_reference():
    """A callback-shaped authorization request still counts as another candidate."""
    a = _credential_addon()
    a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='login')
    for number in (1, 2):
        event = await _record_flow(a, _flow(host='login.example', sni='login.example', headers=NAV,
            path='/auth?client_id=client&redirect_uri=https://login.example/auth'))
        assert event.binding['candidate'] == number


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['install', 'revoke', 'end_login', 'end_manual'])
async def test_authority_change_after_window_closed_keeps_raw_events(change):
    """An expired close deadline is never reopened by a later authority change."""
    a = _credential_addon()
    if change == 'end_login':
        a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
    elif change == 'end_manual':
        a.begin_manual(Lease('m', 'r1', 's1', 1, 'manual'), purpose='assist')
    a.close_window(a.token)
    a.clock.advance(4)
    if change == 'install':
        a.install(replace(a.policy, revision=2))
    elif change == 'revoke':
        a.revoke('r1')
    elif change == 'end_login':
        a.end_login('l')
    else:
        a.end_manual('m')
    assert not a.window_open
    value = canary().encode()
    event = await _record_flow(a, _flow(method='POST', host='outside.example',
                                       sni='outside.example', content=value))
    assert event.origin == 'https://outside.example'      # recorded, not withheld


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no_header', 'header', 'closed', 'reopened',
                                  'other_session', 'unlisted', 'other_region', 'subdomain', 'websocket',
                                  'http', 'wrong_port'])
async def test_guest_sign_in_hosts_are_fixed(case):
    """Fixed exact hosts need no capability; a supplied header grants no authority."""
    from casa_browser.infra import onepassword_extension_hosts
    a = _credential_addon()
    a.extension_hosts = onepassword_extension_hosts('acct.1password.eu')
    token = a.open_window()
    headers = dict(NAV)
    host = 'my.1password.eu'
    if case in ('header', 'unlisted', 'other_region', 'subdomain'):
        headers['x-casa-guest-window'] = f'{token[0]}:{token[1]}'
    if case == 'closed':
        a.close_window(token)
        a.clock.advance(4)
    elif case == 'reopened':
        a.open_window()
    elif case == 'other_session':
        a.install(replace(a.policy, run_id='other'))
    elif case == 'unlisted':
        host = 'unlisted.1password.eu'
    elif case == 'other_region':
        host = 'my.1password.com'
    elif case == 'subdomain':
        host = 'extra.my.1password.eu'
    elif case == 'websocket':
        host = 'b5n.1password.eu'
        headers.update({'upgrade': 'websocket', 'connection': 'Upgrade'})
    flow = _flow(host=host, sni=host, path='/signin?auth-only=1', headers=headers)
    if case == 'http':
        flow.request.scheme = 'http'
        flow.request.port = 80
    elif case == 'wrong_port':
        flow.request.port = 8443
        flow.request.host_header = host + ':8443'
    event = await _record_flow(a, flow)
    assert event.allow is (case not in ('unlisted', 'other_region', 'subdomain', 'http', 'wrong_port'))
    if 'x-casa-guest-window' in headers:
        assert flow.request.headers['x-casa-guest-window'] == headers['x-casa-guest-window']
    if case == 'no_header':
        assert event.origin == 'https://my.1password.eu'


def test_guest_sign_in_exact_hosts_without_capability(tmp_path):
    """Fixed-host traffic reaches upstream without a header; a header cannot widen it."""
    from casa_browser.infra import onepassword_extension_hosts

    async def scenario():
        hosts = onepassword_extension_hosts('acct.1password.eu')
        async with Harness(tmp_path, [*HOSTS, *hosts], account_hosts=hosts) as hz:
            hz.addon.install(account_policy())
            token = await ProxyClient(hz.control_path).open_window()
            for host in sorted(hosts):
                assert (await hz.fetch('GET', 'https://' + host + '/signin?auth-only=1',
                                      headers=NAV))[0] == 200
            value = canary()
            assert (await hz.fetch('POST', 'https://my.1password.eu/guest',
                headers={**FETCH, 'sec-fetch-site': 'same-origin'}, data=value.encode()))[0] == 200
            assert hz.fixture.receipts.requests[-1] == ('POST', 'my.1password.eu', '/guest', value.encode())
            for event in hz.addon.events:
                assert value not in json.dumps(encode_event(event))
            await ProxyClient(hz.control_path).close_window(token)
            assert (await hz.fetch('GET', 'https://my.1password.eu/signin', headers=NAV))[0] == 200
            hz.fixture.receipts.reset()
            assert (await hz.fetch('GET', 'https://unlisted.1password.eu/signin',
                headers={**NAV, 'x-casa-guest-window': f'{token[0]}:{token[1]}'}))[0] == 403
            assert nothing_reached(hz)
    run(scenario)


def test_a_refused_begin_login_keeps_the_armed_login_close():
    """A begin_login refused for another run changes nothing: the armed close of the current
    login lease stays armed."""
    a = _credential_addon()
    a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
    a.arm_login_close('l')
    armed = a.login_close_armed_at
    with pytest.raises(LookupError, match='wrong_run'):
        a.begin_login(Lease('x', 'r2', 's1', 1, 'login'))
    assert a.login_close_armed_at == armed is not None
    assert a.policy.login_lease.lease_id == 'l'

def test_a_reinstall_keeps_a_live_view_lease_and_its_purpose_while_the_window_is_open():
    """A reinstall that keeps a login live view's lease keeps its purpose and passes; ending
    the lease while the window is open is not refused."""
    a = _credential_addon()
    view = Lease('m', 'r1', 's1', 1, 'manual')
    a.begin_manual(view, purpose='login')
    assert a.window_open
    a.install(dataclasses.replace(a.policy, revision=a.policy.revision + 1))
    assert a.policy.manual_lease == view and a.manual_purpose == 'login'
    a.end_manual('m')
    assert a.policy.manual_lease is None and a.manual_purpose is None


def test_install_bound_to_proxy_start(tmp_path):
    """Task 12: an install that names a proxy start other than this one (the proxy restarted
    since the controller read it) is refused and leaves the policy as it was; the matching
    start installs."""
    from casa_browser.proxy_main import Proxy
    a = _credential_addon()
    before = a.policy
    p = Proxy(addon=a, confdir=tmp_path, control_path=tmp_path / 'control', controller_uid=os.getuid())
    fresh = encode_policy(dataclasses.replace(account_policy(), revision=7))
    stale = p.dispatch(json.dumps({'op': 'install', 'policy': fresh, 'start': 'older-start'}).encode())
    assert stale == {'ok': False, 'error': 'restarted'} and a.policy is before
    extra = p.dispatch(json.dumps({'op': 'install', 'policy': fresh, 'start': a.start_nonce,
                                   'more': 1}).encode())
    assert extra == {'ok': False, 'error': 'unsupported'} and a.policy is before
    ok = p.dispatch(json.dumps({'op': 'install', 'policy': fresh, 'start': a.start_nonce}).encode())
    assert ok == {'ok': True, 'revision': 7} and a.policy.revision == 7
    assert a.start_nonce == a.token[0]


def test_a_plain_http_address_of_a_site_is_redirected_to_https():
    """casa-test F16: a plain-http GET to a host the site registered over https is answered by
    the proxy itself with a 307 to the https address — no connection, recorded as the quiet
    ``https_upgrade`` refusal; another host over http is still the empty 403."""
    a = _addon()
    a.install(account_policy())
    f = _flow(host="site.example", path="/login?next=1", port=80, scheme="http", sni=None)
    asyncio.run(a.request(f))
    assert f.response is not None and f.response.status_code == 307
    assert f.response.headers["location"] == "https://site.example/login?next=1"
    assert f.response.content == b""
    assert a.events[-1].reason == "https_upgrade" and not a.events[-1].allow
    assert a.pins.fresh("site.example", 80) is None        # nothing was resolved or dialled
    other = _flow(host="unlisted.1password.eu", path="/", port=80, scheme="http", sni=None)
    asyncio.run(a.request(other))
    assert other.response is not None and other.response.status_code == 403   # control
    assert a.events[-1].reason == "site_lock"


# -- slice 5r: the end of a login lease cuts nothing the policy after it admits ------------

SCRIPT = {"sec-fetch-dest": "script", "sec-fetch-mode": "no-cors", "sec-fetch-site": "same-origin"}


@pytest.mark.parametrize('lease_only', [False, True])
def test_end_login_closes_only_what_the_lease_alone_admitted(tmp_path, lease_only):
    """Phone round 10 (enablebanking.com): the broker returned right after the sign-in page's
    first load and ``end_login`` closed every browser connection, cutting the page's own
    scripts and calls in flight; the page stayed on a spinner. Requests in flight that the
    policy after the lease admits (the site's script, a cross-site call of its page) now
    complete. Once the lease admitted what the policy without it refuses (a provider's sign-in
    page), its end still closes every connection, a replaced lease included."""
    async def scenario():
        async with Harness(tmp_path, HOSTS,
                           binding_key_path=tmp_path / 'proxy' / 'binding.key') as hz:
            a = hz.addon
            provider, page = _provider_site(hz)
            a.install(account_policy(sites=(provider,)))
            a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
            if lease_only:
                assert (await hz.fetch('GET', page, headers=NAV))[0] == 200
                assert a.lease_only_admitted
                a.begin_login(Lease('l2', 'r1', 's1', 1, 'login'))   # the broker's replacement
            gate = hz.fixture.response_hold = asyncio.Event()
            hz.fixture.hold_paths = {'/app.js', '/v1/params'}
            hz.fixture.receipts.reset()
            held = [asyncio.create_task(hz.fetch('GET', 'https://login.example/app.js',
                                                 headers=SCRIPT)),
                    asyncio.create_task(hz.fetch('POST', 'https://other.example/v1/params',
                                                 headers=FETCH, data=b'{}'))]
            try:
                for _ in range(300):
                    if len(hz.fixture.receipts.requests) == 2:
                        break
                    await asyncio.sleep(0.01)
                assert len(hz.fixture.receipts.requests) == 2
                assert a.lease_only_admitted is lease_only
                a.end_login('l2' if lease_only else 'l')
                assert a.policy.login_lease is None and not a.lease_only_admitted
                # the close takes effect on the proxy's loop: give it the time before the site
                # answers (a closed connection fails its request; an open one waits)
                await asyncio.wait(held, timeout=1)
                gate.set()
                statuses = [(await asyncio.wait_for(t, 5))[0] for t in held]
            finally:
                gate.set()
                hz.fixture.response_hold = hz.fixture.hold_paths = None
                await asyncio.gather(*held, return_exceptions=True)
            seen = [r[2] for r in hz.fixture.receipts.requests]
            if lease_only:
                # both connections closed: the call fails, and the client sends the GET again
                # on a new connection (aiohttp's RFC 9112 retry of an idempotent request)
                assert statuses == [200, None] and seen.count('/app.js') == 2
            else:
                assert statuses == [200, 200] and seen.count('/app.js') == 1
            assert seen.count('/v1/params') == 1
            # without the lease the provider page is an ordinary refused navigation
            assert (await hz.fetch('GET', page, headers=NAV))[0] == 403
    run(scenario)


@pytest.mark.parametrize('then', ['next_lease', 'same_lease'])
def test_lease_only_mark_ends_only_with_a_close_of_every_connection(tmp_path, then):
    """r1 Astra, r2 Astra and Terra: a provider request admitted under the lease and still
    pending (its upstream handshake held) survives the landing-document closure, which closes
    nothing. Its mark stays, so the next ``end_login`` — the next lease's, or the cleanup's call
    naming the lease that closure already ended, through the control socket — closes every
    connection and the request never reaches the provider; the mark ends with that close."""
    async def scenario():
        async with Harness(tmp_path, HOSTS,
                           binding_key_path=tmp_path / 'proxy' / 'binding.key') as hz:
            a = hz.addon
            provider, page = _provider_site(hz)
            a.install(account_policy(sites=(provider,)))
            a.begin_login(Lease('l', 'r1', 's1', 1, 'login'))
            gate = hz.fixture.hold = asyncio.Event()
            pending = asyncio.create_task(hz.fetch('GET', page, headers=NAV))
            try:
                for _ in range(300):
                    if hz.fixture.receipts.connections and a.lease_only_admitted:
                        break
                    await asyncio.sleep(0.01)
                assert a.lease_only_admitted
                hz.fixture.hold = None
                a.arm_login_close('l')
                assert (await hz.fetch('GET', 'https://login.example/landing', headers=NAV))[0] == 200
                assert a.policy.login_lease is None and a.lease_only_admitted
                # still held: the provider request has not reached the site
                assert not any(r[1] == 'provider.example' for r in hz.fixture.receipts.requests)
                if then == 'next_lease':
                    a.begin_login(Lease('m', 'r1', 's1', 1, 'login'))
                    a.end_login('m')
                else:
                    await ProxyClient(hz.control_path).end_login('l')
                assert not a.lease_only_admitted
                gate.set()
                assert (await asyncio.wait_for(pending, 5))[0] != 200
                assert not any(r[1] == 'provider.example' for r in hz.fixture.receipts.requests)
            finally:
                gate.set()
                hz.fixture.hold = None
                await asyncio.gather(pending, return_exceptions=True)
            # with the mark gone, the next lease's end cuts nothing the policy admits
            a.begin_login(Lease('n', 'r1', 's1', 1, 'login'))
            gate = hz.fixture.response_hold = asyncio.Event()
            hz.fixture.hold_paths = {'/app.js'}
            hz.fixture.receipts.reset()
            held = asyncio.create_task(hz.fetch('GET', 'https://login.example/app.js',
                                                headers=SCRIPT))
            try:
                for _ in range(300):
                    if hz.fixture.receipts.requests:
                        break
                    await asyncio.sleep(0.01)
                a.end_login('n')
                await asyncio.wait([held], timeout=1)
                assert not held.done()
                gate.set()
                assert (await asyncio.wait_for(held, 5))[0] == 200
                assert [r[2] for r in hz.fixture.receipts.requests].count('/app.js') == 1
            finally:
                gate.set()
                hz.fixture.response_hold = hz.fixture.hold_paths = None
                await asyncio.gather(held, return_exceptions=True)
    run(scenario)
