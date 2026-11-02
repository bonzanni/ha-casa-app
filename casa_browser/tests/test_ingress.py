"""Task 13: the ingress boundary — nginx admits only Home Assistant's ingress peer, under any
ingress prefix, for pages and both live-view sockets."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from casa_browser import ui
from uihelpers import HEADERS, Operator, operator, rig, waiting_run  # noqa: F401


APP = Path(__file__).resolve().parents[1]
CONF = APP / "rootfs" / "etc" / "nginx" / "casa_browser.conf"
UNITS = APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"


def _directives(text: str) -> list[str]:
    return [line.split("#", 1)[0].strip() for line in text.splitlines()
            if line.split("#", 1)[0].strip()]


def test_nginx_conf():
    lines = _directives(CONF.read_text(encoding="utf-8"))
    assert "listen 8099;" in lines and sum(1 for l in lines if l.startswith("listen")) == 1
    assert "include /run/casa-browser/nginx/ingress-peer.conf;" in lines
    assert not any(l.startswith(("allow", "deny")) for l in lines)   # only the rendered file
    assert "access_log off;" in lines and "error_log stderr crit;" in lines
    assert "server unix:/run/casa-browser/ui/ui.sock;" in lines
    assert "proxy_set_header Upgrade $http_upgrade;" in lines
    assert "proxy_set_header Connection $connection_upgrade;" in lines
    assert not any("proxy_pass http://127" in l or "proxy_pass http://localhost" in l for l in lines)
    assert not any(re.search(r"\blocation\s+/novnc/\s*\{", l) for l in lines)  # no noVNC app shell


def test_direct_ingress_request_denied():
    """nginx admits exactly one peer — Supervisor's ingress address — and denies everyone else,
    whatever a request's headers claim (the decision is the TCP peer's address)."""
    assert ui.ingress_peer() == ".".join(map(str, (172, 30, 32, 2)))
    assert ui.ingress_peer_conf() == f"allow {ui.ingress_peer()};\ndeny all;\n"
    run = (UNITS / "svc-browser-nginx" / "run").read_text(encoding="utf-8")
    assert "python -m casa_browser.ui ingress-peer" in run
    assert "s6-setuidgid casa-ingress nginx -c /etc/nginx/casa_browser.conf" in run
    vnc = (UNITS / "svc-browser-vnc" / "run").read_text(encoding="utf-8")
    for flag in ("-viewonly", "-rfbport 0", "-rfbportv6 -1", "-unixsock /run/casa-browser/vnc/vnc.sock", "-nopw"):
        assert flag in vnc
    assert "s6-setuidgid casa-vnc x11vnc" in vnc


@pytest.mark.parametrize("prefix", ["/api/hassio_ingress/AbC_token-1",
                                    "/api/hassio_ingress/" + "Z" * 128])
async def test_prefix_http_and_websocket(rig, operator, prefix, monkeypatch, tmp_path):
    """Under an arbitrary ingress prefix: a 200 page whose every URL carries the prefix, and a
    101 upgrade of both live-view sockets."""
    import uihelpers
    monkeypatch.setattr(uihelpers, "PREFIX", prefix)
    monkeypatch.setitem(uihelpers.HEADERS, "X-Ingress-Path", prefix)

    async def handle(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        await reader.read(10)
        writer.close()
    server = await asyncio.start_unix_server(handle, path=str(rig.tmp / "vnc.sock"))
    run_id = await waiting_run(rig)
    status, html, _ = await operator.get("/")
    assert status == 200
    assert all(h.startswith(prefix + "/") for h in re.findall(r'(?:href|action|src)="([^"]+)"', html))
    viewer = await operator.open_view(run_id)
    for kind in ("ctl", "websockify"):
        ws = await operator.ws(run_id, kind, viewer)
        assert not ws.closed                      # the 101 upgrade succeeded
        await ws.close()
    server.close()
