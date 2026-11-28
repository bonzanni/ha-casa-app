"""Task 13: the ingress boundary — nginx admits only Home Assistant's ingress peer, under any
ingress prefix, for pages and both live-view sockets."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

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


# -- the saved-logins page (the app's own store) -----------------------------------------------

LOGINS_CONF = "/run/casa-browser/nginx/logins.conf"


def _blocks(text: str) -> dict[str, list[str]]:
    """Each ``location`` of an nginx fragment, by its path, with the directives inside it."""
    out, current = {}, None
    for line in _directives(text):
        found = re.fullmatch(r"location\s+(\S+)\s*\{", line)
        if found:
            current = out.setdefault(found.group(1), [])
        elif current is not None:
            current.append(line)
    return out


def test_the_saved_logins_locations_are_included_after_the_peer_rule():
    lines = _directives(CONF.read_text(encoding="utf-8"))
    server = lines.index("server {")
    peer = lines.index("include /run/casa-browser/nginx/ingress-peer.conf;")
    logins = lines.index(f"include {LOGINS_CONF};")
    first_location = next(i for i, l in enumerate(lines) if l.startswith("location"))
    assert server < peer < logins < first_location


def _public_options(tmp_path, login_store):
    import json
    path = tmp_path / "public_options.json"
    if login_store is not None:
        path.write_text(json.dumps({"language": None, "login_store": login_store}))
    return path


def test_the_saved_logins_locations_exist_only_with_the_app_s_own_store(tmp_path):
    for store in ("onepassword", "none", None):
        assert ui.logins_conf(_public_options(tmp_path, store)) == "", store
    blocks = _blocks(ui.logins_conf(_public_options(tmp_path, "builtin")))
    assert set(blocks) == {"/logins/static/", "/logins/"}
    static, page = blocks["/logins/static/"], blocks["/logins/"]
    assert "alias /opt/casa_browser/vault_static/;" in static
    assert {"text/css css;", "application/javascript js;"} <= set(static)
    assert 'add_header Cache-Control "no-store" always;' in static
    assert 'add_header X-Content-Type-Options "nosniff" always;' in static
    assert "proxy_pass http://unix:/run/casa-browser/logins/page.sock:/;" in page
    for header in ("X-Ingress-Path $http_x_ingress_path", "X-Forwarded-Proto $http_x_forwarded_proto",
                   "X-Forwarded-Host $http_x_forwarded_host"):
        assert f"proxy_set_header {header};" in page, header
    assert "client_max_body_size 300k;" in page


def test_the_nginx_unit_writes_the_saved_logins_locations(tmp_path, monkeypatch, capsys):
    """The unit writes the include file from the options bootstrap wrote for the controller,
    the way it writes the peer rule, before nginx starts."""
    run = (UNITS / "svc-browser-nginx" / "run").read_text(encoding="utf-8")
    write = f"python -m casa_browser.ui logins-conf > {LOGINS_CONF}.tmp"
    move = f"mv {LOGINS_CONF}.tmp {LOGINS_CONF}"
    assert write in run and move in run
    assert run.index(write) < run.index(move) < run.index("exec env -i")
    from casa_browser import options
    for store in ("builtin", "onepassword"):
        path = _public_options(tmp_path, store)
        monkeypatch.setattr(options, "Paths", lambda path=path: SimpleNamespace(public_options=path))
        assert ui.main(["ui", "logins-conf"]) == 0
        assert capsys.readouterr().out == ui.logins_conf(path), store


def test_the_broker_unit_makes_the_page_s_directory_for_nginx():
    """In both modes, before the broker drops to its own user: the directory nginx's group can
    pass through and the broker owns, and a failure to make it does not stop the broker."""
    run = (UNITS / "svc-browser-broker" / "run").read_text(encoding="utf-8")
    make = "install -d -o casa-broker -g casa-ingress -m 2710 /run/casa-browser/logins || true"
    assert make in run.splitlines()
    assert run.index(make) < run.index("s6-setuidgid casa-broker")
