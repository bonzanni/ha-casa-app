"""Shared helpers for the ingress UI and live-view tests: an "operator's browser" that talks to
``ui.make_ui`` the way Home Assistant ingress delivers requests (prefix stripped, X-Ingress-Path
and the forwarded origin set), with the real controller over Task 12's doubles."""
from __future__ import annotations

import re

import pytest

from casa_browser.ui import COOKIE, make_ui
from controllerfakes import ACCOUNT, Rig

PREFIX = "/api/hassio_ingress/AbC_token-1"
ORIGIN = "https://ha.test"
HEADERS = {"X-Ingress-Path": PREFIX, "X-Forwarded-Host": "ha.test", "X-Forwarded-Proto": "https",
           "X-Remote-User-Id": "user1"}
A_START = ACCOUNT.start_page_private


class Operator:
    """One browser: its cookie, the last page it saw (forms and nonces come from there)."""

    def __init__(self, client):
        self.client = client
        self.cookie: str | None = None
        self.html = ""

    def headers(self, extra=None, *, origin=True) -> dict:
        h = dict(HEADERS)
        if origin:
            h["Origin"] = ORIGIN
        if self.cookie:
            h["Cookie"] = f"{COOKIE}={self.cookie}"
        h.update(extra or {})
        return h

    async def get(self, path: str, **kw):
        resp = await self.client.get(path, headers=self.headers(kw.pop("headers", None), origin=False),
                                     allow_redirects=False, **kw)
        text = await resp.text() if resp.content_type.startswith(("text/", "application/json")) else ""
        if resp.content_type == "text/html":
            self.html = text
        return resp.status, text, resp

    def nonce(self, path: str, html: str | None = None) -> str | None:
        page = self.html if html is None else html
        action = re.escape(PREFIX + path)
        m = re.search(r'<form method="post" action="' + action + r'"[^>]*>'
                      r'<input type="hidden" name="nonce" value="([^"]+)">', page)
        return m.group(1) if m else None

    async def post(self, path: str, data: dict | None = None, *, nonce="auto", headers=None,
                   origin=True):
        body = dict(data or {})
        if nonce == "auto":
            nonce = self.nonce(path)
            assert nonce is not None, f"no form for {path} on the last page"
        if nonce is not None:
            body["nonce"] = nonce
        resp = await self.client.post(path, data=body, headers=self.headers(headers, origin=origin),
                                      allow_redirects=False)
        text = await resp.text()
        if resp.content_type == "text/html":
            self.html = text
        cookie = resp.cookies.get(COOKIE)
        if cookie is not None:
            self.cookie = cookie.value
            self.cookie_attrs = cookie
        return resp.status, text, resp

    async def follow(self, resp):
        location = resp.headers["Location"]
        assert location.startswith(PREFIX)
        return await self.get(location[len(PREFIX):])

    async def open_view(self, run_id: str):
        """Home → Open → the viewer page; returns its viewer id."""
        await self.get("/")
        status, _, resp = await self.post(f"/runs/{run_id}/open")
        assert status == 303, self.html
        status, html, _ = await self.follow(resp)
        assert status == 200, html
        return re.search(r'data-viewer="([^"]+)"', html).group(1)

    async def ws(self, run_id: str, kind: str, viewer: str | None, **kw):
        return await self.client.ws_connect(f"/view/{run_id}/{kind}?v={viewer or ''}",
                                            headers=self.headers(), **kw)


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.live.end_all()
    await r.ctl.shutdown()
    r.close()


@pytest.fixture
async def operator(rig, aiohttp_client):
    client = await aiohttp_client(make_ui(rig.ctl))
    return Operator(client)


async def waiting_run(rig, purpose="login", **kw):
    run = await rig.run(**kw)
    assert run.state == "running", run.state
    await rig.ctl.request_live(run.run_id, purpose)
    await rig.settle()
    assert rig.state(run.run_id) == "waiting_live"
    return run.run_id
