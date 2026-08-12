# tests/test_admin_peercred_gate.py
"""#467 — SO_PEERCRED root gate on the internal socket's /admin/* family.

The gate is an aiohttp middleware (``internal_handlers.admin_peercred_
middleware``) attached to the internal Unix-socket app in
``casa_core.start_internal_unix_runner``. It rejects any ``/admin/*`` request
whose connecting peer is not uid 0, and fails CLOSED when the peer identity
cannot be read. Non-``/admin/*`` routes pass through untouched.

These tests bind a real Unix socket so SO_PEERCRED reports the true peer uid
(the test process — non-root in dev/CI), which exercises the rejection path
without needing to run as root; the allow path is exercised by monkeypatching
the peer-uid probe to report 0.
"""
from __future__ import annotations

import os

import aiohttp
import pytest
from aiohttp import web

import internal_handlers

pytestmark = pytest.mark.asyncio


async def _serve(tmp_path, monkeypatch, *, peer_uid_override=...):
    """Start the middleware over a Unix socket app with /admin + /internal
    echo routes. Returns (session, base_connector_path, cleanup)."""
    if peer_uid_override is not ...:
        monkeypatch.setattr(
            internal_handlers, "_peer_uid",
            lambda _request: peer_uid_override,
        )

    async def admin_handler(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "reached": "admin"})

    async def internal_handler(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "reached": "internal"})

    app = web.Application(
        middlewares=[internal_handlers.admin_peercred_middleware])
    app.router.add_post("/admin/reload", admin_handler)
    app.router.add_post("/internal/tools/call", internal_handler)

    sock_path = str(tmp_path / "internal.sock")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, sock_path)
    await site.start()

    connector = aiohttp.UnixConnector(path=sock_path)
    session = aiohttp.ClientSession(connector=connector)

    async def cleanup():
        await session.close()
        await runner.cleanup()

    return session, cleanup


async def test_admin_route_rejects_non_root_peer(tmp_path, monkeypatch) -> None:
    # Real peer uid = the test process, which is not root in dev/CI.
    if os.getuid() == 0:
        pytest.skip("test process is root; cannot exercise the reject path")
    session, cleanup = await _serve(tmp_path, monkeypatch)
    try:
        resp = await session.post(
            "http://localhost/admin/reload", json={"scope": "policies"})
        assert resp.status == 403
        body = await resp.json()
        assert body["kind"] == "forbidden"
    finally:
        await cleanup()


async def test_admin_route_allows_root_peer(tmp_path, monkeypatch) -> None:
    session, cleanup = await _serve(tmp_path, monkeypatch, peer_uid_override=0)
    try:
        resp = await session.post(
            "http://localhost/admin/reload", json={"scope": "policies"})
        assert resp.status == 200
        body = await resp.json()
        assert body["reached"] == "admin"
    finally:
        await cleanup()


async def test_admin_route_fails_closed_on_unknown_peer(
        tmp_path, monkeypatch) -> None:
    # Identity unavailable (None) must be treated as non-root.
    session, cleanup = await _serve(tmp_path, monkeypatch, peer_uid_override=None)
    try:
        resp = await session.post(
            "http://localhost/admin/reload", json={"scope": "policies"})
        assert resp.status == 403
    finally:
        await cleanup()


async def test_internal_route_not_gated(tmp_path, monkeypatch) -> None:
    # A non-root peer (override to a high uid) still reaches /internal/*.
    session, cleanup = await _serve(
        tmp_path, monkeypatch, peer_uid_override=200001)
    try:
        resp = await session.post(
            "http://localhost/internal/tools/call", json={"name": "x"})
        assert resp.status == 200
        body = await resp.json()
        assert body["reached"] == "internal"
    finally:
        await cleanup()


async def test_unmatched_admin_path_still_404s(tmp_path, monkeypatch) -> None:
    """An /admin/* path with no registered route keeps its 404 for a non-root
    peer — the gate only fires on a genuinely-matched admin route, so a stray
    403 never masks a real not-found."""
    session, cleanup = await _serve(tmp_path, monkeypatch, peer_uid_override=None)
    try:
        resp = await session.post(
            "http://localhost/admin/does-not-exist", json={})
        assert resp.status == 404
    finally:
        await cleanup()


async def test_peer_uid_reads_real_socket_credentials(
        tmp_path, monkeypatch) -> None:
    """The unmocked probe returns the true peer uid over a Unix socket."""
    seen: dict = {}

    async def capture_handler(request: web.Request) -> web.Response:
        seen["uid"] = internal_handlers._peer_uid(request)
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/internal/probe", capture_handler)
    sock_path = str(tmp_path / "probe.sock")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, sock_path)
    await site.start()
    connector = aiohttp.UnixConnector(path=sock_path)
    session = aiohttp.ClientSession(connector=connector)
    try:
        await session.post("http://localhost/internal/probe", json={})
        assert seen["uid"] == os.getuid()
    finally:
        await session.close()
        await runner.cleanup()
