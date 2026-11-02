"""Task 3: the ingress-armed two-minute pairing window."""
from __future__ import annotations

import asyncio

from casa_browser.api import make_app
from casa_browser.pairing import PAIR_WINDOW

from apihelpers import Clock, headers, make_fake, run_body
from fake_controller import FakeController


async def _claim(client, label="Casa (Fido plugin)", protocol="1"):
    return await client.post("/v1/pair/claim", json={"caller_label": label},
                             headers=headers(None, protocol))


async def test_claim_outside_window_unauthorized(aiohttp_client, tmp_path):
    fake = make_fake(tmp_path)
    client = await aiohttp_client(make_app(fake))
    resp = await _claim(client)
    assert (resp.status, await resp.json()) == (401, {"error": "unauthorized"})


async def test_pair_window_atomic(aiohttp_client, tmp_path):
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    client = await aiohttp_client(make_app(fake))
    deadline = fake.pairing.arm()
    assert deadline == clock.now + PAIR_WINDOW and PAIR_WINDOW.total_seconds() == 120
    replies = await asyncio.gather(*(_claim(client, f"claimant-{n}") for n in range(2)))
    statuses = sorted(r.status for r in replies)
    assert statuses == [200, 401]
    winner = next(r for r in replies if r.status == 200)
    body = await winner.json()
    assert set(body) == {"secret", "protocol_version"} and body["protocol_version"] == 1
    assert (await (await client.get("/v1/hello", headers=headers(body["secret"]))).json())["protocol_version"] == 1
    # Exactly at the deadline the window is closed; just before it is open.
    fake.pairing.arm()
    clock.advance(120)
    assert (await _claim(client)).status == 401
    fake.pairing.arm()
    clock.advance(119.999)
    assert (await _claim(client)).status == 200


async def test_claim_needs_protocol_header_and_keeps_window(aiohttp_client, tmp_path):
    fake = make_fake(tmp_path)
    client = await aiohttp_client(make_app(fake))
    fake.pairing.arm()
    for protocol in (None, "2"):
        resp = await _claim(client, protocol=protocol)
        assert (resp.status, await resp.json()) == (400, {"error": "protocol_mismatch"})
    resp = await client.post("/v1/pair/claim", json={"caller_label": "x", "extra": 1},
                             headers=headers(None))
    assert resp.status == 400
    assert fake.pairing.window_open()
    assert (await _claim(client)).status == 200
    assert not fake.pairing.window_open()


async def test_rotate_invalidates_old_secret(aiohttp_client, tmp_path):
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    client = await aiohttp_client(make_app(fake))
    fake.pairing.arm()
    old = (await (await _claim(client)).json())["secret"]
    h = headers(old)
    running = await (await client.post("/v1/runs", json=run_body("a"), headers=h)).json()
    queued = await (await client.post("/v1/runs", json=run_body("b"), headers=h)).json()
    assert (running["state"], queued["state"]) == ("running", "queued")
    # Re-pair: the old secret stops at once, before anyone claims the new one.
    fake.pairing.arm()
    assert (await client.get("/v1/hello", headers=h)).status == 401
    assert fake.store.get_run(running["run_id"]).state == "interrupted"
    assert fake.store.get_run(queued["run_id"]).state == "interrupted"
    new = (await (await _claim(client)).json())["secret"]
    assert new != old
    h2 = headers(new)
    resp = await client.post(f"/v1/runs/{running['run_id']}/observe", json={}, headers=h2)
    assert resp.status == 409  # the run died with the old authority
    assert fake.live == {}
    assert (await client.get("/v1/hello", headers=h2)).status == 200
    assert fake.pairing.generation() == 2


async def test_live_grant_revoked_on_rotation(aiohttp_client, tmp_path):
    fake = make_fake(tmp_path)
    client = await aiohttp_client(make_app(fake))
    fake.pairing.arm()
    old = (await (await _claim(client)).json())["secret"]
    run = await (await client.post("/v1/runs", json=run_body(), headers=headers(old))).json()
    resp = await client.post(f"/v1/runs/{run['run_id']}/live", json={"purpose": "login"},
                             headers=headers(old))
    assert resp.status == 201 and fake.live
    fake.pairing.arm()
    assert fake.live == {}


def test_pair_caller_label_is_untrusted(tmp_path):
    fake = make_fake(tmp_path)
    fake.pairing.arm()
    assert fake.pairing.claim('<script>alert("x")</script>', "172.30.32.9")
    view = fake.pairing.status_view()
    assert "<script>" not in view["caller_label_html"]
    assert view["caller_label_html"].startswith("&lt;script&gt;")
    assert view["caller_label_is_self_reported"] is True
    assert view["observed_peer_html"] == "172.30.32.9"
    assert view["paired"] is True and view["armed"] is False
    assert "secret" not in view and all("secret" not in k for k in view)


def test_window_closed_unclaimed_is_visible(tmp_path):
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    fake.pairing.arm()
    clock.advance(121)
    view = fake.pairing.status_view()
    assert view["window_closed_unclaimed"] is True and view["paired"] is False


async def test_pair_restart_closed(aiohttp_client, tmp_path):
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    fake.pairing.arm()
    fake.store.close()
    # App restart inside the window: the window does not survive.
    restarted = FakeController(tmp_path / "state.db", clock=clock)
    assert not restarted.pairing.window_open()
    client = await aiohttp_client(make_app(restarted))
    assert (await _claim(client)).status == 401


def test_verify_constant_and_unpaired(tmp_path):
    fake = make_fake(tmp_path)
    assert not fake.pairing.verify(None)
    assert not fake.pairing.verify("")
    assert not fake.pairing.verify("anything")
    fake.pairing.arm()
    secret = fake.pairing.claim("Casa", None)
    assert fake.pairing.verify(secret)
    assert not fake.pairing.verify(secret + "x")
    # The store never holds the secret in clear.
    row = fake.store._db.execute("SELECT * FROM pairing").fetchone()
    assert secret not in " ".join(str(v) for v in tuple(row))


def test_claim_exact_deadline_direct(tmp_path):
    """The window check inside ``claim`` holds on its own, not only through the API guard."""
    clock = Clock()
    fake = make_fake(tmp_path, clock)
    fake.pairing.arm()
    clock.advance(120)
    assert fake.pairing.claim("late", None) is None
    fake.pairing.arm()
    clock.advance(119)
    assert fake.pairing.claim("in time", None)
    assert fake.pairing.claim("second", None) is None


async def test_rearm_revokes_a_request_still_arriving(aiohttp_server, tmp_path):
    """A POST authenticated with the old secret whose body is still arriving when the
    operator re-pairs is refused, not run (review r3-c1-r1)."""
    import json as _json

    fake = make_fake(tmp_path)
    fake.pairing.arm()
    old = fake.pairing.claim("Casa", None)
    server = await aiohttp_server(make_app(fake))
    body = _json.dumps(run_body("in-flight")).encode()
    reader, writer = await asyncio.open_connection(server.host, server.port)
    head = (f"POST /v1/runs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {old}\r\n"
            f"X-Casa-Browser-Protocol: 1\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    writer.write(head + body[:1])
    await writer.drain()
    await asyncio.sleep(0.05)
    fake.pairing.arm()
    writer.write(body[1:])
    await writer.drain()
    reply = await reader.read()
    writer.close()
    assert reply.startswith(b"HTTP/1.1 401")
    assert fake.store.list_runs(("queued", "starting", "running")) == []
