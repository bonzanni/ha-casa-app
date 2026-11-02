"""Task 13 in the image: nginx (casa-ingress) in front of the controller's UI socket, x11vnc
(casa-vnc) behind the controller's bridge, and the W+ viewer in a real (test-only) Chrome with
phone emulation, reached through a stand-in for Home Assistant's ingress proxy.

The container's loopback gets Supervisor's ingress address, so a request can come from it as
Supervisor's do; every other peer must be refused by nginx itself. The stand-in strips the
ingress prefix and sets the headers Home Assistant sets (X-Ingress-Path, X-Forwarded-*); it is
not Home Assistant evidence — the real-phone check through HA is the operator's (Task 14)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from probes import as_user
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker

PEER = ".".join(str(o) for o in (172, 30, 32, 2))   # Supervisor's ingress address
PORT = 8099
TOKEN = "it-ingress-token"
PREFIX = f"/api/hassio_ingress/{TOKEN}"
CONTROLLER_SERVICE = "/run/service/svc-browser-controller"
VNC_SOCKET = "/run/casa-browser/vnc/vnc.sock"
LOGIN_PAGE = (b"<title>Sign in</title><h1>Sign in</h1><form>"
              b'<input id="email" aria-label="email" type="email" autocomplete="username" style="font-size:20px;width:320px">'
              b'<br><input id="pw" aria-label="pw" type="password" autocomplete="current-password" style="font-size:20px;width:320px">'
              b"</form>")


@pytest.fixture(scope="module")
def ingress_peer():
    added = subprocess.run(["ip", "addr", "add", f"{PEER}/32", "dev", "lo"], capture_output=True)
    yield PEER
    if added.returncode == 0:
        subprocess.run(["ip", "addr", "del", f"{PEER}/32", "dev", "lo"], capture_output=True)


def _http(host: str, path: str, headers: dict, *, source: str | None = None,
          method: str = "GET") -> tuple[int, bytes, bytes]:
    s = socket.create_connection((host, PORT), timeout=10,
                                 source_address=(source, 0) if source else None)
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    s.sendall(f"{method} {path} HTTP/1.1\r\nHost: ha.test\r\n{head}Connection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    top, _, body = data.partition(b"\r\n\r\n")
    return int(top.split(b" ")[1]), top, body


def test_non_ingress_peer_refused(ingress_peer):
    """Loopback and the container's own network address are not Supervisor: nginx refuses
    them (403) whatever headers they forge, before the UI sees anything."""
    forged = {"X-Ingress-Path": PREFIX, "X-Forwarded-Host": "ha.test",
              "X-Remote-User-Id": "admin", "X-Forwarded-For": PEER}
    own = socket.gethostbyname(socket.gethostname())
    for host, source in (("127.0.0.1", None), (own, own)):
        status, top, body = _http(host, "/", forged, source=source)
        assert status == 403 and b"Casa Browser" not in body, (host, top)
        status, _, _ = _http(host, "/view/run_x/ctl?v=x",
                             {**forged, "Upgrade": "websocket", "Connection": "Upgrade",
                              "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZQ=="},  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
                             source=source)
        assert status == 403
    status, _, body = _http(PEER, "/", {"X-Ingress-Path": PREFIX, "X-Forwarded-Host": "ha.test"},
                            source=PEER)
    assert status == 200 and b"Casa Browser" in body          # the positive control
    status, _, body = _http(PEER, "/", {"X-Forwarded-Host": "ha.test"}, source=PEER)
    assert status == 403                                       # Supervisor always sends it


def test_static_and_novnc_only(ingress_peer):
    ok = {"/static/live.js": b"Reconnect", "/novnc/core/rfb.js": b"export default class RFB"}
    for path, needle in ok.items():
        status, top, body = _http(PEER, path, {}, source=PEER)
        assert status == 200 and needle in body, path
        assert b"javascript" in top.lower()
    for path in ("/novnc/app/ui.js", "/novnc/vnc.html", "/static/../ui.py"):
        status, _, _ = _http(PEER, path, {}, source=PEER)
        assert status in (400, 403, 404), path


def test_vnc_socket_reaches_only_the_controller():
    """x11vnc listens on no TCP port; its socket admits the controller (casa-vncview), not
    the browser, the broker or nginx; it runs as casa-vnc, view-only."""
    tcp = Path("/proc/net/tcp").read_text() + Path("/proc/net/tcp6").read_text()
    assert ":170C " not in tcp                                 # 5900
    ps = subprocess.run(["ps", "-ww", "-o", "user=,args=", "-C", "x11vnc"], capture_output=True,
                        text=True).stdout
    assert "casa-vnc" in ps and "-viewonly" in ps and "-rfbport 0" in ps
    code = ("import socket,sys\ns=socket.socket(socket.AF_UNIX)\n"
            f"try:\n s.connect('{VNC_SOCKET}'); print(s.recv(12))\n"
            "except OSError as e: print('refused', e.errno)")
    for user in ("browser", "broker", "ingress"):
        out = as_user(user, code).stdout.strip()
        assert out.startswith("refused"), (user, out)
    r = subprocess.run(["setpriv", "--reuid=casa-controller", "--regid=casa-controller",
                        "--init-groups", "/opt/venv/bin/python", "-c", code],
                       capture_output=True, text=True, timeout=30)
    assert "RFB 003" in r.stdout, r.stdout + r.stderr


# --------------------------------------------------------------------------- the viewer


class Standin:
    """Home Assistant ingress in miniature: ``/api/hassio_ingress/<token>/<path>`` → nginx at
    ``<peer>:8099/<path>`` from the peer address, with X-Ingress-Path and X-Forwarded-*; HTTP
    and WebSocket. Like Home Assistant core (measured on casa-test, 2026-10-22) it replaces the
    app's ``Referrer-Policy`` with ``no-referrer`` and adds ``X-Frame-Options: SAMEORIGIN`` on
    every response — with ``no-referrer`` a browser sends ``Origin: null`` on a form POST
    unless the document itself says otherwise. ``blackhole()`` makes every open WebSocket
    silent in both directions without closing it — as an iOS app switch leaves them."""

    def __init__(self):
        self.dead = set()
        self.bridges = set()
        self.runner = None
        self.port = None

    async def start(self):
        import aiohttp
        from aiohttp import web
        connector = aiohttp.TCPConnector(local_addr=(PEER, 0), force_close=True)
        self.session = aiohttp.ClientSession(connector=connector, auto_decompress=False)

        async def handle(request: web.Request):
            path = request.match_info["path"]
            url = f"http://{PEER}:{PORT}/{path}"
            if request.query_string:
                url += "?" + request.query_string
            skip = {"host", "content-length", "transfer-encoding", "connection", "upgrade",
                    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
                    "sec-websocket-protocol", "accept-encoding"}
            headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
            headers.update({"X-Ingress-Path": PREFIX, "X-Forwarded-Host": request.host,
                            "X-Forwarded-Proto": "http", "X-Forwarded-For": "127.0.0.1"})
            if request.headers.get("Upgrade", "").lower() == "websocket":
                protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
                down = web.WebSocketResponse(protocols=protocols, max_msg_size=0)
                try:
                    up = await self.session.ws_connect(url, headers=headers, protocols=protocols,
                                                       max_msg_size=0)
                except aiohttp.WSServerHandshakeError as err:
                    return web.Response(status=err.status)
                await down.prepare(request)
                bridge = object()
                self.bridges.add(bridge)

                async def pipe(src, dst):
                    async for msg in src:
                        if bridge in self.dead:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                    if bridge not in self.dead:
                        await dst.close(code=src.close_code or 1000)
                await asyncio.wait([asyncio.ensure_future(pipe(down, up)),
                                    asyncio.ensure_future(pipe(up, down))],
                                   return_when=asyncio.FIRST_COMPLETED)
                return down
            body = await request.read()
            async with self.session.request(request.method, url, headers=headers, data=body,
                                            allow_redirects=False) as resp:
                data = await resp.read()
                out = web.Response(status=resp.status, body=data)
                for k, v in resp.headers.items():
                    if k.lower() not in ("content-length", "transfer-encoding", "connection",
                                         "content-encoding", "server", "date", "referrer-policy",
                                         "x-frame-options"):
                        out.headers.add(k, v)
                out.headers["Referrer-Policy"] = "no-referrer"     # as HA core rewrites it
                out.headers["X-Frame-Options"] = "SAMEORIGIN"
                return out

        app = web.Application()
        app.router.add_route("*", "/api/hassio_ingress/" + TOKEN + "/{path:.*}", handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    def blackhole(self):
        self.dead |= self.bridges

    async def stop(self):
        await self.session.close()
        await self.runner.cleanup()


class Probe:
    def __init__(self, home: Path):
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt:/opt/casa_browser_tests/tests",
               "HOME": str(home), "DISPLAY": ":99",
               "XAUTHORITY": "/run/casa-browser/x-ctl/Xauthority"}
        self.proc = subprocess.Popen(
            ["setpriv", "--reuid=casa-controller", "--regid=casa-controller", "--init-groups",
             "/opt/venv/bin/python", str(Path(__file__).parent / "ingressprobe.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env)

    def line(self, timeout=120):
        import selectors
        with selectors.DefaultSelector() as sel:
            sel.register(self.proc.stdout, selectors.EVENT_READ)
            if not sel.select(timeout):
                raise RuntimeError("probe silent: " + self.stderr())
        text = self.proc.stdout.readline()
        if not text:
            raise RuntimeError("probe ended: " + self.stderr())
        return json.loads(text)

    def ask(self, op, arg=None):
        self.proc.stdin.write(json.dumps({"op": op, "arg": arg}) + "\n")
        self.proc.stdin.flush()
        return self.line()["out"]

    def stderr(self):
        try:
            return self.proc.stderr.read()[-2000:] if self.proc.poll() is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(30)
            except Exception:  # noqa: BLE001
                self.proc.kill()


@contextlib.contextmanager
def controller_service_stopped():
    subprocess.run(["/command/s6-svc", "-wd", "-T", "20000", "-d", CONTROLLER_SERVICE], check=True)
    try:
        yield
    finally:
        subprocess.run(["/command/s6-svc", "-wU", "-T", "60000", "-u", CONTROLLER_SERVICE],
                       check=False)
        # Wait until the restarted controller finished its start (its recovery closes any
        # Chrome of the browser user, which would cut a later test's browser): its API
        # answers only after that.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", 8098), timeout=2).close()
                break
            except OSError:
                time.sleep(0.5)


CONSOLE: list[str] = []
DIAG = ("() => window.__lv ? JSON.stringify({ctl: __lv.ctl, vnc: __lv.vnc, open: __lv.open(), "
        "state: !!__lv.state(), status: __lv.status(), sent: __lv.sent.slice(-8)}) : 'no __lv'")


async def _until(page, expr, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await page.evaluate(expr, isolated_context=False):
            return True
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out: {expr} — {await page.evaluate(DIAG, isolated_context=False)} — {CONSOLE[-10:]}")


async def _url(page, pattern):
    try:
        await page.wait_for_url(pattern, timeout=30_000)
    except Exception:
        raise AssertionError(f"not at {pattern}: {page.url} {(await page.content())[:800]} "
                             f"{CONSOLE[-5:]}") from None


async def _tap_field(page, label: str, end: bool = False) -> None:
    """A tap in the picture inside the field the viewer's last state names ``label`` — where
    the operator's finger would land (the viewer's own remote-to-page mapping); ``end``: near its
    right edge, so the click puts the caret after the field's text."""
    await _until(page, f"__lv.state() && __lv.state().fields.some(f => f.lb === {json.dumps(label)})")
    point = await page.evaluate(f"""() => {{ const f = __lv.state().fields.find(f => f.lb === {json.dumps(label)});
        return __lv.at({'f.x + f.w - 12' if end else 'f.x + Math.min(40, f.w / 2)'}, f.y + f.h / 2); }}""",
        isolated_context=False)
    await page.touchscreen.tap(*point)


async def _session_on(page) -> dict:
    """The phone's keyboard input has the focus and its session is confirmed on the field the
    tap reached (live view K); returns the session."""
    await _until(page, "__lv.kb().focused && __lv.kb().sess && __lv.kb().sess.ok === true && !__lv.kb().want")
    return await page.evaluate("__lv.kb()", isolated_context=False)


async def _tap_into(page, label: str, end: bool = False, tries: int = 2) -> dict:
    """Tap a field and wait for its session to be confirmed. When the click did not reach it
    (the 1Password extension's inline menu under a username field can cover the next field —
    the viewer then says "Tap missed"), tap again, as the operator would; returns the session
    with how many taps it took."""
    for n in range(1, tries + 1):
        await _tap_field(page, label, end)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            k = await page.evaluate("__lv.kb()", isolated_context=False)
            if k["sess"] is None:
                break
            if k["focused"] and k["sess"]["ok"] is True and not k["want"]:
                return dict(k, taps=n)
            await asyncio.sleep(0.2)
    raise AssertionError(f"no confirmed session on {label} after {tries} taps — {CONSOLE[-5:]}")


async def _close_kb(page) -> None:
    await page.evaluate("document.querySelector('#kb').blur()", isolated_context=False)
    await _until(page, "!__lv.kb().sess")


async def _viewer_scenario(probe: Probe, run_id: str) -> dict:
    from patchright.async_api import async_playwright
    standin = await Standin().start()
    base = f"http://127.0.0.1:{standin.port}{PREFIX}"
    result: dict = {}
    frames: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path="/opt/google/chrome/chrome",
                                           headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 390, "height": 664},
                                            is_mobile=True, has_touch=True, device_scale_factor=2)
        page = await context.new_page()
        page.on("console", lambda m: CONSOLE.append(f"{m.type}: {m.text}"[:300]))
        page.on("pageerror", lambda e: CONSOLE.append(f"pageerror: {e}"[:300]))
        page.on("websocket", lambda ws: ws.on("framereceived",
                                              lambda f: frames.append(f if isinstance(f, str) else "")))
        home = await page.goto(base + "/")
        # the positive control of the referrer case: the page arrived with HA's no-referrer,
        # and its forms still post (Open below, Done later) because the document says
        # same-origin itself
        result["referrer_policy"] = home.headers.get("referrer-policy")
        await page.click("form[action$='/open'] button")
        try:
            await page.wait_for_url("**/view/" + run_id, timeout=30_000)
        except Exception:
            raise AssertionError("Open did not reach the viewer: " + page.url + " "
                                 + (await page.content())[:800]) from None
        await page.evaluate("window.__marker = 'same-page'", isolated_context=False)
        await _until(page, "window.__lv && __lv.open()[0] && __lv.open()[1] && __lv.state() && __lv.status() === ''")
        state = await page.evaluate("__lv.state()", isolated_context=False)
        result["fields"] = sorted(f["type"] for f in state["fields"])
        result["window_live"] = probe.ask("window")
        # live view K: a tap in the picture is a click in Chrome; the phone's keyboard input takes
        # the focus, the session is confirmed on the field Chrome focused, and the keys land live
        result["inputs"] = await page.evaluate(
            "[...document.querySelectorAll('input')].filter(i => i.type !== 'hidden').map(i => i.id)",
            isolated_context=False)
        await _tap_field(page, "email")
        k = await _session_on(page)
        result["after_email_tap"] = [probe.ask("active"), k["type"]]
        await page.keyboard.type("me@example.com", delay=15)
        k = await _tap_into(page, "pw")
        result["after_pw_tap"] = [probe.ask("active"), k["type"]]
        await page.keyboard.type("pw-canary-91", delay=15)
        result["values"] = await _value(probe, 1, "pw-canary-91")
        await page.keyboard.press("Enter")            # closes the keyboard; nothing submitted
        await _until(page, "!__lv.kb().sess")
        # the email field again: the click puts the caret where it lands (its end here)
        await _tap_field(page, "email", end=True)
        await _session_on(page)
        await page.keyboard.type("+cb", delay=15)
        result["composed"] = await _value(probe, 0, "me@example.com+cb")
        await _close_kb(page)
        # Chrome's address bar (casa-test F13): with Chrome's toolbar shown, a tap on the address
        # bar raises the keyboard; the first keys replace the address, return goes
        await page.click("#host")
        await asyncio.sleep(1.0)
        st = await page.evaluate("__lv.state()", isolated_context=False)
        point = await page.evaluate("([X, Y]) => __lv.at(X, Y)", [st["win"][0] + 250, st["origin"][1] - 20],
                                    isolated_context=False)
        await page.touchscreen.tap(*point)
        await _until(page, "__lv.kb().focused && __lv.kb().sess && __lv.kb().sess.mode === 'bar'")
        await page.keyboard.type("https://site-a.example/login?via=bar", delay=10)
        await page.keyboard.press("Enter")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            typed_to = probe.ask("url")
            if typed_to.endswith("via=bar"):
                break
            await asyncio.sleep(0.3)
        result["address_bar"] = typed_to
        await page.click("#host")
        await _until(page, "__lv.state() && __lv.state().fields.length >= 2")
        # an app switch: both sockets go silent without closing; then the app comes back
        standin.blackhole()
        await asyncio.sleep(1.0)
        await page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
            Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""", isolated_context=False)
        result["status_during"] = await page.evaluate("__lv.status()", isolated_context=False)
        await _until(page, "__lv.ctl >= 2 && __lv.vnc >= 2 && __lv.open()[0] && __lv.open()[1] && __lv.status() === ''", 40)
        result["marker"] = await page.evaluate("window.__marker", isolated_context=False)
        result["reconnects"] = await page.evaluate("__lv.reconnects", isolated_context=False)
        result["state_after_reconnect"] = probe.ask("state")
        # two return-to-app events at once (r4 Astra S2): one reconnect wins, nothing ends
        before = await page.evaluate("__lv.ctl", isolated_context=False)
        await page.evaluate("""() => {
            for (let i = 0; i < 3; i++) document.dispatchEvent(new Event('visibilitychange'));
            dispatchEvent(new Event('online'));
        }""", isolated_context=False)
        await _until(page, f"__lv.ctl > {before} && __lv.open()[0] && __lv.open()[1] && __lv.status() === ''", 40)
        await asyncio.sleep(2.0)
        result["overlap"] = await page.evaluate(
            "[__lv.open(), __lv.status(), document.querySelector('#cover').hidden]", isolated_context=False)
        # Done (one tap, slice 5g): the sockets close, the window is restored, the run goes on
        # and the closing page says so
        await page.click("#done")
        await page.wait_for_selector("text=Fido is continuing")
        result["window_review"] = probe.ask("window")
        result["state_done"] = probe.ask("state")
        await browser.close()
    await standin.stop()
    result["password_in_frames"] = any("pw-canary-91" in f for f in frames)
    result["frames"] = len(frames)
    return result


TYPING_PAGE = (
    b"<title>Typing</title><form action='/typed' method='get'>"
    b"<input id='search' name='search' aria-label='Search' style='font-size:20px;width:320px'>"
    b"<div style='height:190px'></div>"
    b"<div style='position:relative;width:340px'><input id='covered' name='covered' aria-label='Covered'"
    b" style='font-size:20px;width:320px'><div onmousedown='event.preventDefault()'"
    b" style='position:absolute;left:0;top:0;width:340px;height:40px;background:rgba(0,0,0,.3)'></div></div>"
    b"<input id='user' name='user' aria-label='User' autocomplete='username' style='font-size:20px;width:320px'>"
    b"<br><input id='pw' name='pw' type='password' aria-label='Password' style='font-size:20px;width:320px'>"
    b"<br><button id='go'>Go</button></form>")


async def _open_viewer(page, base: str, run_id: str) -> None:
    await page.goto(base + "/")
    await page.click("form[action$='/open'] button")
    await _url(page, "**/view/" + run_id)
    await _until(page, "window.__lv && __lv.open()[0] && __lv.open()[1] && __lv.state() && __lv.status() === ''")


async def _value(probe: Probe, index: int, want: str, timeout: float = 20) -> list:
    deadline = time.monotonic() + timeout
    values = probe.ask("values")
    while time.monotonic() < deadline and values[index] != want:
        await asyncio.sleep(0.3)
        values = probe.ask("values")
    return values


async def _tap_page_element(page, probe: Probe, element_id: str) -> None:
    """A tap in the picture on a page element that is no field (a button): its page rectangle
    from the page, placed on Chrome's screen by the viewer's last state."""
    r = probe.ask("eval", f"() => {{ const r = document.getElementById({json.dumps(element_id)}).getBoundingClientRect();"
                          " return [r.left + r.width / 2, r.top + r.height / 2]; }")
    point = await page.evaluate("([x, y]) => { const o = __lv.state().origin; return __lv.at(o[0] + x, o[1] + y); }",
                                r, isolated_context=False)
    await page.touchscreen.tap(*point)


async def _typing_scenario(probe: Probe, run_id: str) -> dict:
    """Live view K through the real viewer and the real controller: F35 (form history present,
    the keyboard up, User then Password low in the picture), F34 (keyboard closed, a tap off the
    field, the field again), the omnibox holding Chrome's keys before a field tap, and the
    binding: a click an overlay takes leaves Chrome's focus elsewhere — nothing typed anywhere.
    Inputs in the page: search, covered, user, pw."""
    from patchright.async_api import async_playwright
    standin = await Standin().start()
    base = f"http://127.0.0.1:{standin.port}{PREFIX}"
    out: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path="/opt/google/chrome/chrome",
                                           headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 390, "height": 664},
                                            is_mobile=True, has_touch=True, device_scale_factor=2)
        page = await context.new_page()
        page.on("console", lambda m: CONSOLE.append(f"{m.type}: {m.text}"[:300]))
        probe.ask("goto", "https://site-a.example/typing")
        await _open_viewer(page, base, run_id)
        # form history: one submit of User = tomsmith, through the page's own Go button
        await _tap_field(page, "User")
        await _session_on(page)
        await page.keyboard.type("tomsmith", delay=15)
        await _value(probe, 2, "tomsmith")
        await page.keyboard.press("Enter")             # closes the keyboard: nothing submitted
        await _until(page, "!__lv.kb().sess")
        out["after_return"] = probe.ask("url")
        await _tap_page_element(page, probe, "go")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "/typed" not in probe.ask("url"):
            await asyncio.sleep(0.3)
        out["submitted"] = probe.ask("url")
        probe.ask("goto", "https://site-a.example/typing")
        await _until(page, "__lv.state() && __lv.state().fields.length === 4")
        # F35: the keyboard takes the bottom of the phone; User, then Password, low in the picture
        await page.set_viewport_size({"width": 390, "height": 420})
        await asyncio.sleep(2.0)
        await _tap_field(page, "User")
        await _session_on(page)
        out["f35_user_active"] = probe.ask("active")
        await page.keyboard.type("tom", delay=15)
        k = await _tap_into(page, "Password")
        out["f35_pw"] = [probe.ask("active"), k["type"]]
        await page.keyboard.type("pw-canary-35", delay=15)
        out["f35_values"] = await _value(probe, 3, "pw-canary-35")
        await _close_kb(page)
        await page.set_viewport_size({"width": 390, "height": 664})
        await asyncio.sleep(1.5)
        # F34: the keyboard closed, a tap off the field, then the field again (at its end)
        off = await page.evaluate("""() => { const f = __lv.state().fields.find(f => f.lb === 'User');
            return __lv.at(f.x + f.w + 70, f.y + f.h / 2); }""", isolated_context=False)
        await page.touchscreen.tap(*off)
        await asyncio.sleep(1.0)
        out["f34_off"] = probe.ask("active")
        await _tap_field(page, "User", end=True)
        await _session_on(page)
        out["f34_again"] = probe.ask("active")
        await page.keyboard.type("+34", delay=15)
        out["f34_values"] = await _value(probe, 2, "tom+34")
        await _close_kb(page)
        # the omnibox holds Chrome's keys (an address-bar tap), then a field tap: the keys land
        # in the field, not in the address bar
        await page.click("#host")
        await asyncio.sleep(1.0)
        st = await page.evaluate("__lv.state()", isolated_context=False)
        bar = await page.evaluate("([X, Y]) => __lv.at(X, Y)", [st["win"][0] + 250, st["origin"][1] - 20],
                                  isolated_context=False)
        await page.touchscreen.tap(*bar)
        await _until(page, "__lv.kb().sess && __lv.kb().sess.mode === 'bar'")
        await asyncio.sleep(0.5)
        await page.click("#host")
        await asyncio.sleep(1.0)
        await _tap_field(page, "Search")
        await _session_on(page)
        out["omni_active"] = probe.ask("active")
        await page.keyboard.type("q1", delay=15)
        out["omni_values"] = await _value(probe, 0, "q1")
        out["omni_url"] = probe.ask("url")
        await _close_kb(page)
        # the binding: Covered lies under an overlay that keeps the focus where it was (as a
        # dropdown does: its mousedown is prevented), so the click lands on the overlay and Chrome's
        # focus stays on Search; what the operator typed for Covered goes nowhere — not to Search,
        # not to any field — and a line says so
        await _tap_field(page, "Search")
        await _session_on(page)
        await _tap_field(page, "Covered")
        await page.keyboard.type("bind-canary", delay=10)
        await _until(page, "!__lv.kb().sess")
        await asyncio.sleep(1.0)
        out["bind_values"] = probe.ask("values")
        out["bind_note"] = await page.evaluate("document.querySelector('#toast').textContent", isolated_context=False)
        await browser.close()
    await standin.stop()
    return out


def test_keys_go_to_the_tapped_field(ingress_peer, tmp_path):
    """Live view K, end to end (F34, F35, the binding): a tap in the picture is a click in Chrome,
    and the phone's keys land live in the field Chrome then focuses — ``document.activeElement``
    — with form history present and the keyboard up, User then Password low in the picture;
    after the keyboard closed and a tap off the field, the field again; after the omnibox held
    Chrome's keys. Return closes the keyboard without submitting (the page's own button does).
    A click an overlay takes leaves Chrome's focus off the tapped field: the keys typed for it go
    nowhere and a line says so."""
    home = tmp_path / "controller-home"
    home.mkdir()
    os.chown(home, 1103, 1103)
    for p in [tmp_path, *tmp_path.parents]:
        if str(p).startswith("/tmp/"):
            p.chmod(p.stat().st_mode | 0o711)
    with ChromeSites(["site-a.example"]) as sites, controller_service_stopped():
        sites.call(setattr, sites.harness.proxy, "controller_uid", 1103)
        sites.pages["/login"] = ("text/html", LOGIN_PAGE, {})
        sites.pages["/typing"] = ("text/html", TYPING_PAGE, {})
        sites.pages["/typed"] = ("text/html", b"<title>Typed</title>typed", {})
        probe = Probe(home)
        try:
            run_id = probe.line()["ready"]
            out = asyncio.run(_typing_scenario(probe, run_id))
        finally:
            probe.close()
    assert out["after_return"] == "https://site-a.example/typing", out    # return submitted nothing
    assert "/typed?search=&covered=&user=tomsmith" in out["submitted"], out
    assert out["f35_user_active"] == "user", out
    assert out["f35_pw"] == ["pw", "password"], out
    assert out["f35_values"] == ["", "", "tom", "pw-canary-35"], out     # nothing in User from Password
    assert out["f34_off"] != "user" and out["f34_again"] == "user", out
    assert out["f34_values"][2] == "tom+34", out
    assert out["omni_active"] == "search" and out["omni_values"][0] == "q1", out
    assert out["omni_url"] == "https://site-a.example/typing", out       # nothing typed in the bar
    assert out["bind_values"] == ["q1", "", "tom+34", "pw-canary-35"], out    # nothing typed anywhere
    assert "missed" in out["bind_note"], out


def test_viewer_end_to_end(ingress_peer, tmp_path):
    """The W+ viewer through nginx: both sockets upgrade under the ingress prefix, a tap in the
    picture focuses the field in Chrome and the typing box's text reaches it (a password field's
    value never comes back to the viewer), the window is fitted then restored at Done, and after both sockets went silent
    (an app switch) the viewer reconnects them by itself, says so, and the run stays live."""
    home = tmp_path / "controller-home"
    home.mkdir()
    os.chown(home, 1103, 1103)
    for p in [tmp_path, *tmp_path.parents]:
        if str(p).startswith("/tmp/"):
            p.chmod(p.stat().st_mode | 0o711)
    with ChromeSites(["site-a.example"]) as sites, controller_service_stopped():
        sites.call(setattr, sites.harness.proxy, "controller_uid", 1103)
        sites.pages["/login"] = ("text/html", LOGIN_PAGE, {})
        probe = Probe(home)
        try:
            run_id = probe.line()["ready"]
            assert probe.ask("state") == "waiting_live"
            initial = probe.ask("window")
            out = asyncio.run(_viewer_scenario(probe, run_id))
            out["window_initial"] = initial
        finally:
            probe.close()
    assert out["referrer_policy"] == "no-referrer"
    assert out["fields"] == ["email", "password"]
    assert out["inputs"] == ["kb"]                                    # nothing over the picture
    assert out["after_email_tap"] == ["email", "text"]                # Chrome's focus: email
    assert out["after_pw_tap"] == ["pw", "password"]                  # pw's masked
    live = out["window_live"]
    assert live["bounds"][2] == 500 and live["bounds"][2] < 1920      # fitted to the phone
    assert live["screen"][:2] == [1920, 1080]                         # the X screen is unchanged
    assert out["values"] == ["me@example.com", "pw-canary-91"], out
    assert out["composed"] == ["me@example.com+cb", "pw-canary-91"]
    assert out["address_bar"] == "https://site-a.example/login?via=bar"
    assert out["password_in_frames"] is False and out["frames"] > 0
    assert out["status_during"] == "Reconnecting…" and out["reconnects"] >= 1
    assert out["marker"] == "same-page" and out["state_after_reconnect"] == "live"
    assert out["overlap"] == [[True, True], "", True]
    assert out["window_review"]["bounds"] == out["window_initial"]["bounds"]
    assert out["window_initial"]["bounds"][2] >= 1919
    assert out["window_review"]["screen"][:2] == [1920, 1080]
    assert out["state_done"] == "running"


@pytest.mark.parametrize("shadow", [False, True])
def test_reveal_follows_the_focus_into_a_frame(shadow):
    """r5 S2 (casa-test F6): a focused field inside an iframe low on the page ends in sight
    after ``reveal`` — the frame scrolled into view in the page, the field in the frame. The
    positive control: before it, the field is below the shrunk viewport."""
    async def scenario():
        from patchright.async_api import async_playwright
        from casa_browser.liveview import reveal
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path="/opt/google/chrome/chrome",
                                               headless=True, args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": 500, "height": 800})
            inner = ("<div style='height:900px'></div><input id=deep style='width:200px'>"
                     "<div style='height:900px'></div>")
            iframe = f"<iframe id=fr style='width:400px;height:600px' srcdoc=\"{inner}\"></iframe>"
            if shadow:   # r8 S2: the iframe inside an open shadow root
                await page.set_content("<div style='height:1400px'>top</div><div id=host></div>"
                                       "<div style='height:1400px'>bottom</div>")
                await page.evaluate("h => { document.querySelector('#host').attachShadow({mode: 'open'})"
                                    ".innerHTML = h; }", iframe)
                await page.wait_for_function("() => document.querySelector('#host').shadowRoot"
                                             ".querySelector('iframe').contentDocument?.querySelector('#deep')")
            else:
                await page.set_content("<div style='height:1400px'>top</div>" + iframe +
                                       "<div style='height:1400px'>bottom</div>")
            frame = page.frame_locator("#host >> #fr" if shadow else "#fr")
            await frame.locator("#deep").focus()
            # both documents back at their top: the frame's own scroll is needed too
            await page.evaluate("window.scrollTo(0, 0)")
            await page.frames[1].evaluate("window.scrollTo(0, 0)")
            await page.set_viewport_size({"width": 500, "height": 300})
            before = await frame.locator("#deep").bounding_box()
            await reveal(page)
            after = await frame.locator("#deep").bounding_box()
            await browser.close()
            return before, after
    before, after = asyncio.run(scenario())
    assert before["y"] > 300, before                     # out of sight before
    assert 0 <= after["y"] and after["y"] + after["height"] <= 300, after


SHRINK = r'''
import asyncio, json, os, sys
sys.path.insert(0, "/opt")
from pathlib import Path
async def main():
    from casa_browser.browser import BrowserManager
    from casa_browser.liveview import ChromeScreen, viewer_state
    from casa_browser.policy import RunPolicy, SessionRef
    from casa_browser.protocol import EffectiveLocale
    from casa_browser.proxy_client import ProxyClient
    await ProxyClient().install(RunPolicy(run_id="it", mode="open_web", site_grants=(), sites=(),
        login_lease=None, manual_lease=None, revision=1,
        active_session=SessionRef("public", None, 1)))
    mgr = BrowserManager(placeholder_dir=Path(os.environ["HOME"]) / "placeholder", gpu=False)
    bs = await mgr.open(None, "open_web", EffectiveLocale("en-GB", "Europe/Amsterdam", "option"))
    try:
        page = bs.page
        await page.goto("https://site-a.example/low", wait_until="domcontentloaded")
        scr = ChromeScreen()
        await scr.fit(bs, 500, 880)
        await asyncio.sleep(1)
        st = await scr.probe(bs)
        low = next(f for f in st["fields"] if f["lb"] == "low")
        await scr.inject(bs, {"t": "tap", "x": int(low["x"] + 20), "y": int(low["y"] + 10)})
        await asyncio.sleep(0.5)
        await scr.fit(bs, 500, 452)              # the phone's keyboard took the room
        await asyncio.sleep(1)
        shrunk = viewer_state(await scr.probe(bs))
        await scr.inject(bs, {"t": "text", "s": "low-typed"})
        await asyncio.sleep(0.5)
        values = await page.evaluate("() => [...document.querySelectorAll('input')].map(i => [i.name, i.value])")
        print("SHRINK", json.dumps({"fields": [[f["lb"], f["focused"], f["y"]] for f in shrunk["fields"]],
                                    "view": shrunk["view"], "origin": shrunk["origin"], "values": values,
                                    "active": await page.evaluate("() => document.activeElement.name")}), flush=True)
    finally:
        await mgr.shutdown()
asyncio.run(main())
'''


def test_the_focused_field_stays_in_the_probe_when_the_window_shrinks(tmp_path):
    """casa-test F31: Chrome's window shrinks for the phone's keyboard; the focused field in the
    lower half is then below the viewport. The probe still lists it (focused, with its
    coordinates) so the typing box keeps naming it; text typed next reaches it.
    Control: an unfocused field equally far down is left out, as before."""
    home = tmp_path / "h"
    home.mkdir()
    os.chown(home, 1103, 1103)
    for p in [tmp_path, *tmp_path.parents]:
        if str(p).startswith("/tmp/"):
            p.chmod(p.stat().st_mode | 0o711)
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt:/opt/casa_browser_tests/tests",
           "HOME": str(home), "DISPLAY": ":99", "XAUTHORITY": "/run/casa-browser/x-ctl/Xauthority"}
    page = (b"<title>Low</title><input name=top aria-label=top>"
            b"<div style='height:560px'></div><input name=low aria-label=low>"
            b"<div style='height:60px'></div><input name=lower aria-label=lower>"
            b"<div style='height:600px'></div>")
    with ChromeSites(["site-a.example"]) as sites, controller_service_stopped():
        sites.call(setattr, sites.harness.proxy, "controller_uid", 1103)
        sites.pages["/low"] = ("text/html", page, {})
        r = subprocess.run(["setpriv", "--reuid=casa-controller", "--regid=casa-controller",
                            "--init-groups", "/opt/venv/bin/python", "-c", SHRINK],
                           capture_output=True, text=True, env=env, timeout=240)
    line = next((l for l in r.stdout.splitlines() if l.startswith("SHRINK ")), None)
    assert line, r.stdout[-1500:] + r.stderr[-1500:]
    out = json.loads(line[len("SHRINK "):])
    names = {f[0]: f for f in out["fields"]}
    view_bottom = out["origin"][1] + out["view"][1]
    assert "low" in names and names["low"][1] is True, out          # kept, and focused
    assert names["low"][2] > view_bottom, out                         # though below the viewport
    assert "lower" not in names, out                                  # control: an unfocused one goes
    assert out["active"] == "low" and ["low", "low-typed"] in out["values"], out


# The guard at the ChromeScreen level, in real Chrome (live view K: taps are clicks): its view of
# Chrome's focus (a page moving it, a field replaced by another), one focused field across sibling
# frames, a cross-origin frame's field, the omnibox holding the keys before a click.
GUARD = r'''
import asyncio, json, os, sys
sys.path.insert(0, "/opt")
from pathlib import Path
async def main():
    from casa_browser.browser import BrowserManager
    from casa_browser.liveview import ChromeScreen
    from casa_browser.policy import RunPolicy, SessionRef
    from casa_browser.protocol import EffectiveLocale
    from casa_browser.proxy_client import ProxyClient
    await ProxyClient().install(RunPolicy(run_id="it", mode="open_web", site_grants=(), sites=(),
        login_lease=None, manual_lease=None, revision=1,
        active_session=SessionRef("public", None, 1)))
    mgr = BrowserManager(placeholder_dir=Path(os.environ["HOME"]) / "placeholder", gpu=False)
    bs = await mgr.open(None, "open_web", EffectiveLocale("en-GB", "Europe/Amsterdam", "option"))
    out = {}
    try:
        page = bs.page
        await page.goto("https://site-a.example/guard", wait_until="load")
        await asyncio.sleep(1.5)
        scr = ChromeScreen()
        await scr.fit(bs, 500, 880)
        await asyncio.sleep(0.5)
        async def fields():
            st = await scr.probe(bs)
            return {f["lb"]: f for f in st["fields"]}
        def key(f):
            return {"fi": f["fi"], "doc": f["doc"], "id": f["id"]}
        async def focus(f):                    # the operator's tap: a click at the field
            await scr.inject(bs, {"t": "tap", "x": int(f["x"] + 20), "y": int(f["y"] + 8)})
            await asyncio.sleep(0.3)
        async def typed(f, text):
            ok = await scr.focused_is(bs, key(f))
            if ok:
                await scr.inject(bs, {"t": "text", "s": text, "to": key(f)})
                await asyncio.sleep(0.4)
            return ok
        vals = "() => [...document.querySelectorAll('input')].map(i => i.id + '=' + i.value)"
        fs = await fields()
        a, b = fs["A"], fs["B"]
        await focus(a)
        out["a_typed"] = await typed(a, "a1")
        await page.evaluate("() => document.getElementById('b').focus()", isolated_context=False)
        out["a_after_page_moved"] = await scr.focused_is(bs, key(a))       # the page moved it
        out["b_typed"] = await typed((await fields())["B"], "b1")
        # A replaced by a new field in its place, which the page focuses: the old key is gone
        await focus(a)
        await page.evaluate("""() => { const old = document.getElementById('a'), n = document.createElement('input');
            n.id = 'a2'; n.setAttribute('aria-label', 'A'); old.replaceWith(n); n.focus(); }""", isolated_context=False)
        out["old_a_after_replace"] = await scr.focused_is(bs, key(a))
        out["new_a_id_differs"] = (await fields())["A"]["id"] != a["id"]
        # sibling frames: focus FA, then FB (cross-origin): one focused field each time
        await focus(fs["FA"])
        out["focused_after_fa"] = sorted(l for l, f in (await fields()).items() if f["focused"])
        # the page takes the focus back to its own B: FA's document may keep its own focused
        # element, but the focus chain no longer leads there
        await page.evaluate("() => document.getElementById('b').focus()", isolated_context=False)
        await asyncio.sleep(0.3)
        out["fa_after_main_b"] = await scr.focused_is(bs, key(fs["FA"]))
        out["fa_own_active"] = await page.frames[1].evaluate("() => (document.activeElement || {}).id || null")
        out["focused_after_main_b"] = sorted(l for l, f in (await fields()).items() if f["focused"])
        await focus(fs["FA"])
        fb = (await fields())["FB"]
        await focus(fb)
        out["focused_after_fb"] = sorted(l for l, f in (await fields()).items() if f["focused"])
        out["fb_typed"] = await typed((await fields())["FB"], "y1")
        out["fa_after_fb"] = await scr.focused_is(bs, key(fs["FA"]))
        # the omnibox holds Chrome's keys; a focus takes them back to the page
        await scr.xdo("key", "ctrl+l")
        await asyncio.sleep(0.4)
        b = (await fields())["B"]
        await focus(b)
        out["b_after_omnibox"] = await typed(b, "-o")
        # a newer page (a popup) is what the operator sees now: B's key no longer reaches the keys
        popup = await bs.context.new_page()
        await popup.goto("https://site-a.example/guard", wait_until="load")
        await asyncio.sleep(0.5)
        out["b_after_popup"] = await scr.focused_is(bs, key(b))
        # r2 (Terra): keys for B sent while the popup is the newest page go to B's own page —
        # inject fronts the field's page, never the newest (the guard checked that one)
        await scr.inject(bs, {"t": "text", "s": "-p", "to": key(b)})
        await asyncio.sleep(0.4)
        out["popup_values"] = await popup.evaluate(vals)
        await popup.close()
        await asyncio.sleep(0.3)
        a2 = (await fields())["A"]
        await focus(a2)
        out["a2_typed"] = await typed(a2, "x")
        out["values"] = await page.evaluate(vals)
        out["frame_values"] = [await f.evaluate(vals) for f in page.frames[1:]]
        out["url"] = page.url
    finally:
        print("GUARD", json.dumps(out), flush=True)
        await mgr.shutdown()
asyncio.run(main())
'''

GUARD_PAGE = (b"<title>Guard</title><input id='a' aria-label='A'><input id='b' aria-label='B'><br>"
              b"<iframe style='height:60px' srcdoc=\"<input id='x' aria-label='FA'>\"></iframe>"
              b"<iframe style='height:60px' src='https://site-b.example/inner'></iframe>")


def _run_as_controller(tmp_path, script: str, pages: dict, marker: str, hosts=("site-a.example",)):
    home = tmp_path / "h"
    home.mkdir()
    os.chown(home, 1103, 1103)
    for p in [tmp_path, *tmp_path.parents]:
        if str(p).startswith("/tmp/"):
            p.chmod(p.stat().st_mode | 0o711)
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt:/opt/casa_browser_tests/tests",
           "HOME": str(home), "DISPLAY": ":99", "XAUTHORITY": "/run/casa-browser/x-ctl/Xauthority"}
    with ChromeSites(list(hosts)) as sites, controller_service_stopped():
        sites.call(setattr, sites.harness.proxy, "controller_uid", 1103)
        for path, body in pages.items():
            sites.pages[path] = ("text/html", body, {})
        r = subprocess.run(["setpriv", "--reuid=casa-controller", "--regid=casa-controller",
                            "--init-groups", "/opt/venv/bin/python", "-c", script],
                           capture_output=True, text=True, env=env, timeout=400)
    line = next((l for l in r.stdout.splitlines() if l.startswith(marker + " ")), None)
    assert line, r.stdout[-1500:] + r.stderr[-1500:]
    return json.loads(line[len(marker) + 1:])


def test_guard_sees_chromes_own_focus(tmp_path):
    """The guard in real Chrome (live view K): text bound to a field is typed only while
    Chrome's focus is that element — not after the page moved the focus, not into a field that
    replaced it (a new id); one focused field across sibling frames (Astra d1), the cross-origin
    one included; after the omnibox held Chrome's keys a click gives them to the page."""
    out = _run_as_controller(tmp_path, GUARD, {"/guard": GUARD_PAGE,
                                               "/inner": b"<input id='y' aria-label='FB'>"},
                             "GUARD", hosts=("site-a.example", "site-b.example"))
    print("guard:", json.dumps(out))
    assert out["a_typed"] is True and out["b_typed"] is True, out
    assert out["a_after_page_moved"] is False, out                  # positive control: a_typed
    assert out["old_a_after_replace"] is False and out["new_a_id_differs"] is True, out
    assert out["focused_after_fa"] == ["FA"] and out["focused_after_fb"] == ["FB"], out
    assert out["fa_after_main_b"] is False and out["focused_after_main_b"] == ["B"], out
    assert out["fb_typed"] is True and out["fa_after_fb"] is False, out
    assert out["b_after_omnibox"] is True, out
    assert out["b_after_popup"] is False, out                       # r1: the keys' page is checked
    assert out["popup_values"] == ["a=", "b="], out                  # r2: nothing typed in the popup
    assert out["a2_typed"] is True, out
    assert out["values"] == ["a2=x", "b=b1-o-p"], out               # 'a1' left with the old A
    assert out["frame_values"] == [["x="], ["y=y1"]], out
    assert out["url"] == "https://site-a.example/guard", out         # nothing typed in the bar


# Slice 5m (phone round 9): the live view's probe and keys reach a field in a frame the page
# wrote (W3Schools' demo frame: Playwright's address about:blank). The probe reads every frame,
# whatever its address: the live view is the operator's own view, not a model's.
LVFRAME = r"""
import asyncio, json, os, sys
sys.path.insert(0, "/opt")
from pathlib import Path
async def main():
    from casa_browser.browser import BrowserManager
    from casa_browser.liveview import ChromeScreen
    from casa_browser.policy import RunPolicy, SessionRef
    from casa_browser.protocol import EffectiveLocale
    from casa_browser.proxy_client import ProxyClient
    await ProxyClient().install(RunPolicy(run_id="it", mode="open_web", site_grants=(), sites=(),
        login_lease=None, manual_lease=None, revision=1,
        active_session=SessionRef("public", None, 1)))
    mgr = BrowserManager(placeholder_dir=Path(os.environ["HOME"]) / "placeholder", gpu=False)
    bs = await mgr.open(None, "open_web", EffectiveLocale("en-GB", "Europe/Amsterdam", "option"))
    out = {}
    try:
        page = bs.page
        await page.goto("https://site-a.example/written", wait_until="load")
        await asyncio.sleep(1.5)
        scr = ChromeScreen()
        await scr.fit(bs, 500, 880)
        await asyncio.sleep(0.5)
        out["frame_urls"] = [f.url for f in page.frames]
        st = await scr.probe(bs)
        fs = {f["lb"]: f for f in st["fields"]}
        out["fields"] = sorted(fs)
        async def typed(label, text):
            f = fs[label]
            await scr.inject(bs, {"t": "tap", "x": int(f["x"] + 6), "y": int(f["y"] + 8)})
            await asyncio.sleep(0.3)
            to = {"fi": f["fi"], "doc": f["doc"], "id": f["id"]}
            ok = await scr.focused_is(bs, to)
            for ch in (text if ok else ""):     # the phone's keyboard: one key per message
                await scr.inject(bs, {"t": "text", "s": ch, "to": to})
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.4)
            return f["fi"] > 0, ok
        out["n"] = await typed("N", "hello")
        # "A": the AM/PM segment when Chrome shows the field in 12 hours (ignored in 24)
        out["t"] = await typed("T", "0745A")
        out["m"] = await typed("M", "0745A")          # control: the same field in the page
        out["main"] = await page.evaluate("() => document.getElementById('m').value")
        out["values"] = await page.frames[1].evaluate(
            "() => [...document.querySelectorAll('input')].map(i => i.id + '=' + i.value)")
    finally:
        print("LVFRAME", json.dumps(out), flush=True)
        await mgr.shutdown()
asyncio.run(main())
"""

WRITTEN_PAGE = (b"<title>Try it</title><input type=time id=m aria-label=M "
                b"style='width:200px;height:40px'><br><iframe id=res style='width:400px;height:120px'></iframe>"
                b"<script>const d = document.getElementById('res').contentWindow.document; d.open();"
                b"d.write(\"<input id=n aria-label=N style='width:200px;height:40px'><br>"
                b"<input type=time id=t aria-label=T style='width:200px;height:40px'>\"); "
                b"d.close();</script>")


def test_the_live_view_types_into_a_field_in_a_written_frame(tmp_path):
    out = _run_as_controller(tmp_path, LVFRAME, {"/written": WRITTEN_PAGE}, "LVFRAME")
    print("lvframe:", json.dumps(out))
    assert out["frame_urls"][1] == "about:blank", out        # the class this pins
    assert {"N", "T"} <= set(out["fields"]), out
    assert out["n"] == [True, True] and out["t"] == [True, True], out   # in the frame, focused
    assert out["main"] == "07:45", out                        # control: the page's own field
    assert out["values"] == ["n=hello", "t=07:45"], out


# The managed policy keeps Chrome's form history (single-field autocomplete) and address and
# card autofill out of the live view (F35: its dropdown covered the next field). The shipped
# keys are AutofillAddressEnabled (marked deprecated in M156 for AutofillSettings, which alone
# does not turn single-field history off) and AutofillCreditCardEnabled: this pins that they
# still do it.
HISTORY = r"""
import asyncio, json, os, sys
sys.path.insert(0, "/opt")
from pathlib import Path
async def main():
    from casa_browser.browser import BrowserManager
    from casa_browser.liveview import ChromeScreen
    from casa_browser.policy import RunPolicy, SessionRef
    from casa_browser.protocol import EffectiveLocale
    from casa_browser.proxy_client import ProxyClient
    await ProxyClient().install(RunPolicy(run_id="it", mode="open_web", site_grants=(), sites=(),
        login_lease=None, manual_lease=None, revision=1,
        active_session=SessionRef("public", None, 1)))
    mgr = BrowserManager(placeholder_dir=Path(os.environ["HOME"]) / "placeholder", gpu=False)
    bs = await mgr.open(None, "open_web", EffectiveLocale("en-GB", "Europe/Amsterdam", "option"))
    out = {}
    try:
        page = bs.page
        scr = ChromeScreen()
        async def user():
            await page.goto("https://site-a.example/hist", wait_until="load")
            await asyncio.sleep(1.0)
            f = next(f for f in (await scr.probe(bs))["fields"] if f["lb"] == "user")
            await scr.inject(bs, {"t": "tap", "x": int(f["x"] + 20), "y": int(f["y"] + 8)})
            await asyncio.sleep(0.3)
        await user()
        await scr.inject(bs, {"t": "text", "s": "tomsmith"})
        await scr.xdo("key", "Return")
        await asyncio.sleep(2)
        out["submitted"] = page.url
        await user()
        await scr.inject(bs, {"t": "text", "s": "t"})
        await asyncio.sleep(0.8)
        await scr.xdo("key", "Down")
        await asyncio.sleep(0.4)
        await scr.xdo("key", "Return")
        await asyncio.sleep(1.5)
        out["after"] = page.url
        if "/hist" in page.url:      # Return picked a suggestion into the field: no submit
            out["value"] = await page.evaluate("() => document.querySelector('input').value")
    finally:
        print("HISTORY", json.dumps(out), flush=True)
        await mgr.shutdown()
asyncio.run(main())
"""


@pytest.mark.parametrize("shipped", [True, False])
def test_form_history_dropdown_is_off(tmp_path, shipped):
    """F35: with the shipped policy, a field with form history offers no dropdown: Down then
    Return submits what was typed (``t``). Positive control: without the two autofill keys the
    same keys pick the remembered ``tomsmith`` into the field (Return takes the suggestion)."""
    policy = Path("/etc/opt/chrome/policies/managed/casa.json")
    original = policy.read_text()
    pol = json.loads(original)
    assert pol["AutofillAddressEnabled"] is False and pol["AutofillCreditCardEnabled"] is False
    if not shipped:
        del pol["AutofillAddressEnabled"], pol["AutofillCreditCardEnabled"]
        policy.write_text(json.dumps(pol))
    try:
        out = _run_as_controller(tmp_path, HISTORY, {
            "/hist": b"<title>H</title><form action='/sent' method='get'><input name='user' aria-label='user'></form>",
            "/sent": b"<title>sent</title>sent"}, "HISTORY")
    finally:
        policy.write_text(original)
    assert out["submitted"].endswith("/sent?user=tomsmith"), out
    if shipped:
        assert out["after"].endswith("/sent?user=t"), out          # no dropdown: what was typed
    else:
        assert out.get("value") == "tomsmith", out                 # the remembered value, picked


# Live view K, plain keyboard (operator ruling after K d3): each phone key event goes to Chrome as
# one key, and Chrome decides what it does. Each mapping is checked against the same key pressed
# through Chrome's own input (a reference field, CDP key events): the results must be equal,
# whatever they are — an emoji sequence, a combining accent, a password field.
PLAIN = r'''
import asyncio, json, os, sys
sys.path.insert(0, "/opt")
from pathlib import Path
async def main():
    from casa_browser.browser import BrowserManager
    from casa_browser.liveview import ChromeScreen
    from casa_browser.policy import RunPolicy, SessionRef
    from casa_browser.protocol import EffectiveLocale
    from casa_browser.proxy_client import ProxyClient
    await ProxyClient().install(RunPolicy(run_id="it", mode="open_web", site_grants=(), sites=(),
        login_lease=None, manual_lease=None, revision=1,
        active_session=SessionRef("public", None, 1)))
    mgr = BrowserManager(placeholder_dir=Path(os.environ["HOME"]) / "placeholder", gpu=False)
    bs = await mgr.open(None, "open_web", EffectiveLocale("en-GB", "Europe/Amsterdam", "option"))
    out = {}
    CDP = {"Backspace": "Backspace", "CtrlBackspace": "Control+Backspace", "Delete": "Delete",
           "Left": "ArrowLeft", "Right": "ArrowRight"}
    try:
        page = bs.page
        await page.goto("https://site-a.example/plain", wait_until="load")
        await asyncio.sleep(1.0)
        scr = ChromeScreen()
        await scr.fit(bs, 500, 880)
        await asyncio.sleep(0.5)
        def value(label):
            return page.evaluate("l => document.querySelector(`[aria-label='${l}']`).value", label,
                                 isolated_context=False)
        async def prepare(label, typed, caret):
            await page.evaluate("([l, v, c]) => { const e = document.querySelector(`[aria-label='${l}']`);"
                                " e.value = v; e.focus(); const n = c === null ? v.length : c; e.setSelectionRange(n, n); }",
                                [label, typed, caret], isolated_context=False)
        async def ours(label, typed, keys, caret=None):
            # the live view's path: a tap (a real click), then text and keys bound to the field
            f = next(f for f in (await scr.probe(bs))["fields"] if f["lb"] == label)
            key = {"fi": f["fi"], "doc": f["doc"], "id": f["id"]}
            await scr.inject(bs, {"t": "tap", "x": int(f["x"] + f["w"] - 10), "y": int(f["y"] + 8)})
            await asyncio.sleep(0.3)
            await prepare(label, "", None)
            for piece in ([typed] if isinstance(typed, str) else typed):   # one message each
                await scr.inject(bs, {"t": "text", "s": piece, "to": key})
            await asyncio.sleep(0.3)
            if caret is not None:
                await prepare(label, await value(label), caret)
            for k in keys:
                await scr.inject(bs, {"t": "key", "k": k, "to": key})
                await asyncio.sleep(0.2)
            return await value(label)
        async def reference(label, typed, keys, caret=None):
            await prepare(label, "".join(typed), caret)
            for k in keys:
                await page.keyboard.press(CDP[k])
            await asyncio.sleep(0.2)
            return await value(label)
        cases = {
            "text": ("T", "héllo wörld \U0001F600", [], None),
            "backspace": ("T", "abc", ["Backspace"], None),
            "word": ("T", "one two", ["CtrlBackspace"], None),
            "delete": ("T", "abc", ["Delete"], 1),
            "left": ("T", "abc", ["Left", "Backspace"], None),
            "right": ("T", "abc", ["Right", "Backspace"], 1),
            "family": ("T", "x\U0001F468‍\U0001F469‍\U0001F467", ["Backspace"], None),
            "emoji_accent": ("T", "x\U0001F600́", ["Backspace"], None),
            "decomposed": ("T", "xé", ["Backspace"], None),
            "password": ("P", "pw\U0001F600́", ["Backspace"], None),
            # accents typed between plain letters, in one message and in several
            "italian": ("T", "perché è già così", [], None),
            "pieces": ("P", ["Pässw", "ö", "rd!9", "é"], [], None),
        }
        await page.evaluate("() => { window.__keyups = [];"
                            " document.addEventListener('keyup', e => __keyups.push(e.key), true); }",
                            isolated_context=False)
        for name, (label, typed, keys, caret) in cases.items():
            mine = await ours(label, typed, keys, caret)
            ref = await reference("R" if label == "T" else "RP", typed, keys, caret)
            out[name] = [mine, ref]
        # the same text again and again: xdotool's spare-keycode binding lost or changed a
        # character in 78 of 360 texts on 1 busy CPU, and on a CI runner (ö and the emoji)
        out["again"] = [await ours("T", "perché è già così 😀", [], None) for _ in range(8)]
        out["keyups"] = await page.evaluate("() => __keyups", isolated_context=False)
    finally:
        print("PLAIN", json.dumps(out), flush=True)
        await mgr.shutdown()
asyncio.run(main())
'''

PLAIN_PAGE = (b"<title>Plain</title><input aria-label='T' style='width:300px'><br>"
              b"<input aria-label='P' type='password' style='width:300px'><br>"
              b"<input aria-label='R' style='width:300px'><br>"
              b"<input aria-label='RP' type='password' style='width:300px'>")


def test_plain_keys_do_in_chrome_what_the_key_does(tmp_path):
    """Live view K, plain keyboard: text, Backspace, Ctrl+Backspace (delete word), Delete and the
    strip's Left / Right,
    sent the live view's way (a click, then keys bound to the field: xdotool for plain ASCII and
    the keys, Chrome's own key events for text with a character the keymap lacks), leave the
    field exactly as the same keys through Chrome's own input do — a family emoji, an emoji with
    a combining accent, a decomposed accent, accents between plain letters in one message and in
    several, a password field: whatever Chrome does. Accented text typed again and again keeps
    every character, and the page sees a key event for an accent too."""
    out = _run_as_controller(tmp_path, PLAIN, {"/plain": PLAIN_PAGE}, "PLAIN")
    print("plain:", json.dumps(out, ensure_ascii=False))
    assert set(out) == {"text", "backspace", "word", "delete", "left", "right", "family",
                        "emoji_accent", "decomposed", "password", "italian", "pieces", "again",
                        "keyups"}, out
    again, keyups = out.pop("again"), out.pop("keyups")
    assert {"é", "è", "à", "ì", "ö", "\U0001F600"} <= set(keyups), keyups   # a key event for each
    for case, (mine, ref) in out.items():
        assert mine == ref, (case, out)
    assert again == ["perché è già così \U0001F600"] * 8, again
    assert out["italian"][0] == "perché è già così" and out["pieces"][0] == "Pässwörd!9é", out
    # positive controls: the keys did something
    assert out["text"][0] == "héllo wörld \U0001F600", out
    assert out["backspace"][0] == "ab" and out["word"][0] == "one " and out["delete"][0] == "ac", out
    assert out["left"][0] == "ac" and out["right"][0] == "ac", out          # the strip's ← → moved the caret
    assert out["family"][0] != "x\U0001F468‍\U0001F469‍\U0001F467", out
