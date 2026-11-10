"""The proxy's memory limit (operator ruling 2026-10-20 on the buffering residual): a buffer
mitmproxy grows without a bound of its own ends at the limit, the proxy exits rather than run
on wedged, and s6 restarts it holding no policy; ordinary concurrent traffic stays under it."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from probes import as_user, install_open_web, probe, revoke

pytestmark = pytest.mark.docker

LIMIT = 1073741824


def _proxy_pid() -> int | None:
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if b"casa_browser.proxy_main" in (p / "cmdline").read_bytes():
                    return int(p.name)
            except OSError:
                pass
    return None


def _status_revision(timeout: float = 30.0) -> str | None:
    """The proxy answers its control socket within ``timeout`` (alive and not wedged)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = as_user("controller", "import asyncio\nfrom casa_browser.proxy_client import ProxyClient\n"
                                  "print(asyncio.run(ProxyClient(timeout=3).status()).get('revision'))")
        if r.returncode == 0:
            return r.stdout.strip()
        time.sleep(0.5)
    return None


def _as_browser(code: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["setpriv", "--reuid=casa-browser", "--regid=casa-browser", "--clear-groups",
                           "/opt/venv/bin/python", "-c", code], capture_output=True, text=True,
                          timeout=timeout, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt"})


def test_the_proxy_runs_under_its_data_limit():
    limits = Path(f"/proc/{_proxy_pid()}/limits").read_text()
    line = next(l for l in limits.splitlines() if l.startswith("Max data size"))
    assert line.split()[3] == str(LIMIT) and line.split()[4] == str(LIMIT), line
    run = Path("/etc/s6-overlay/s6-rc.d/svc-browser-proxy/run").read_text()
    # /proc/<pid>/environ is unreadable here, even to root
    assert "MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_=262144" in run


def test_many_concurrent_resolutions_stay_under_the_limit():
    """Ordinary concurrency (every resolver thread a malloc arena) must not reach the limit."""
    install_open_web(revision=5)
    before = _proxy_pid()
    code = r'''
import asyncio
async def one(i):
    try:
        r, w = await asyncio.open_connection("127.0.0.1", 3128)
        w.write(f"CONNECT casa-it-{i}.invalid.example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        await asyncio.wait_for(r.read(100), 20)
        w.close()
    except Exception:
        pass
async def main():
    await asyncio.gather(*(one(i) for i in range(192)))
asyncio.run(main())
print("done")
'''
    assert "done" in _as_browser(code).stdout
    assert _proxy_pid() == before
    assert _status_revision() == "5"
    revoke()


def _vm_data_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmData:"):
            return int(line.split()[1])
    return 0


def _peak_while(pid: int, code: str) -> tuple[str, int]:
    """Run ``code`` as the browser user; the proxy's peak data segment meanwhile, in kB."""
    peak, done = [0], threading.Event()

    def sample():
        while not done.is_set():
            try:
                peak[0] = max(peak[0], _vm_data_kb(pid))
            except OSError:
                return
            time.sleep(0.02)

    sampler = threading.Thread(target=sample)
    sampler.start()
    try:
        out = _as_browser(code, timeout=300).stdout
    finally:
        done.set()
        sampler.join()
    return out, peak[0]


def test_repeated_concurrent_downloads_stay_well_under_the_limit():
    """An image-heavy page, three times: 100 concurrent 2 MB responses per round. No response is
    buffered in the proxy, so its memory stays far below the limit and nothing is retained
    from one round to the next (a buffered body stays until garbage collection reaches it)."""
    install_open_web(revision=9)
    pid = _proxy_pid()
    code = r"""
import socket, ssl, threading, collections
host, path = "speed.cloudflare.com", "/__down?bytes=2000000"
res = collections.Counter(); lock = threading.Lock()
def one():
    try:
        s = socket.create_connection(("127.0.0.1", 3128), timeout=120)
        s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        assert b" 200" in s.recv(4096).split(b"\r\n")[0]
        t = ssl.create_default_context(cafile="/run/casa-browser/ca/ca.pem").wrap_socket(s, server_hostname=host)
        t.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0\r\nSec-Fetch-Dest: image\r\nSec-Fetch-Mode: no-cors\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while True:
            c = t.recv(1 << 20)
            if not c: break
            data += c
        head, _, body = data.partition(b"\r\n\r\n")
        r = f"{head.split(b' ')[1].decode()}:{len(body)}"
    except Exception as e:
        r = "err:" + type(e).__name__
    with lock: res[r] += 1
ts = [threading.Thread(target=one) for _ in range(100)]; [t.start() for t in ts]; [t.join() for t in ts]
print(dict(res))
"""
    base = _vm_data_kb(pid)
    peaks = []
    for _ in range(3):
        out, peak = _peak_while(pid, code)
        assert out.strip() == "{'200:2000000': 100}", out
        peaks.append(peak - base)
    assert _proxy_pid() == pid, "the proxy exited during ordinary downloads"
    # kB above the starting point; buffered responses added ~680 MB in one round
    assert max(peaks) < 256 * 1024, (base, peaks)
    revoke()


def test_repeated_concurrent_uploads_stay_well_under_the_limit():
    """Form posts and uploads, three times: 100 concurrent 1 MB POSTs per round. The event ring
    keeps each request's decision for the controller, bounded by its total size as well as by
    count, and large buffers are returned to the system when freed, so the
    proxy's memory stays below the limit and does not grow from round to round."""
    install_open_web(revision=11)
    pid = _proxy_pid()
    code = r"""
import socket, ssl, threading, collections
host, path, size = "speed.cloudflare.com", "/__up", 1000000
res = collections.Counter(); lock = threading.Lock(); body = b"x" * size
def one():
    try:
        s = socket.create_connection(("127.0.0.1", 3128), timeout=120)
        s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        assert b" 200" in s.recv(4096).split(b"\r\n")[0]
        t = ssl.create_default_context(cafile="/run/casa-browser/ca/ca.pem").wrap_socket(s, server_hostname=host)
        t.sendall(f"POST {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0\r\nSec-Fetch-Dest: empty\r\nSec-Fetch-Mode: cors\r\nContent-Type: application/octet-stream\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n".encode() + body)
        data = b""
        while True:
            c = t.recv(65536)
            if not c: break
            data += c
        r = data.split(b" ")[1].decode() if data else "empty"
        if r != "200":     # who answered: the upstream (its Server header) or the proxy itself
            head = data.split(b"\r\n\r\n", 1)[0].lower()
            r += " from " + next((l.split(b":", 1)[1].strip().decode() for l in head.split(b"\r\n")
                                  if l.startswith(b"server:")), "no server header")
    except Exception as e:
        r = "err:" + type(e).__name__
    with lock: res[r] += 1
ts = [threading.Thread(target=one) for _ in range(100)]; [t.start() for t in ts]; [t.join() for t in ts]
print(dict(res))
"""
    base = _vm_data_kb(pid)
    peaks, settled = [], []
    for _ in range(3):
        out, peak = _peak_while(pid, code)
        assert out.strip() == "{'200': 100}", out
        peaks.append(peak - base)
        time.sleep(3)                       # past one timed collection
        settled.append(_vm_data_kb(pid) - base)
    assert _proxy_pid() == pid, "the proxy exited during ordinary uploads"
    # kB above the starting point. A round's peak is its 100 requests in flight (measured
    # ~270-520 MB, varying with the upstream); what stays after a round must not grow from
    # round to round (an unbounded ring kept ~200 MB per round and ended the proxy;
    # uncollected bodies ~100 MB per round).
    assert max(peaks) < 640 * 1024, (base, peaks, settled)
    assert settled[2] - settled[0] < 128 * 1024, (base, peaks, settled)
    revoke()


@pytest.mark.parametrize("flood", ["http1_head", "http2_data"])
def test_a_flood_ends_at_the_limit_and_the_proxy_fails_closed(flood):
    install_open_web(revision=7)
    before = _proxy_pid()
    if flood == "http1_head":
        code = r'''
import socket
s = socket.create_connection(("127.0.0.1", 3128), timeout=30)
s.sendall(b"GET http://example.com/ HTTP/1.1\r\nX-Pad: ")
chunk = b"a" * (1 << 20)
try:
    for _ in range(2500):
        s.sendall(chunk)
except OSError:
    pass
print("done")
'''
    else:
        code = r'''
import socket, ssl, h2.connection, h2.config, h2.events
s = socket.create_connection(("127.0.0.1", 3128), timeout=30)
s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
assert b" 200" in s.recv(4096).split(b"\r\n")[0]
ctx = ssl.create_default_context(cafile="/run/casa-browser/ca/ca.pem")
ctx.set_alpn_protocols(["h2"])
t = ctx.wrap_socket(s, server_hostname="example.com")
c = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
c.initiate_connection()
c.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 2**31 - 1})
t.sendall(c.data_to_send())
c.send_headers(1, [(":method", "GET"), (":scheme", "https"), (":authority", "example.com"), (":path", "/")])
t.sendall(c.data_to_send())
chunk = b"a" * 16384
sent = 0
try:
    while sent < 2500 * (1 << 20):
        window = min(c.local_flow_control_window(1), len(chunk), c.max_outbound_frame_size)
        if window <= 0:
            for ev in c.receive_data(t.recv(65536)):
                pass
            t.sendall(c.data_to_send())
            continue
        c.send_data(1, chunk[:window])
        t.sendall(c.data_to_send())
        sent += window
except Exception:
    pass
print("done")
'''
    _as_browser(code, timeout=300)
    revision = _status_revision()
    assert revision is not None, "the proxy is wedged: its control socket does not answer"
    # Exited at the limit and restarted by s6: no policy, so it fails closed until reinstalled.
    assert _proxy_pid() != before
    assert revision == "None"
    assert probe("browser", "https_via_proxy", "example.com", 443) == "status:403"
    revoke()
