"""Task 5, in the built image: the kernel floor confines every service user.

Runs inside the app container started with the image's own s6 /init (run-integration.sh),
so the floor was installed by the production oneshot and the proxy is the production
service. Every denied probe has a root positive control on the same destination, so an
unreachable destination cannot pass as "refused"."""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from probes import PY, install_open_web, probe, revoke

pytestmark = pytest.mark.docker

TARGET_HOST, TARGET_PORT = os.environ.get("CASA_IT_PRIVATE_TARGET", "0.0.0.0:0").rsplit(":", 1)
TARGET_PORT = int(TARGET_PORT)
PUBLIC = ("1.1.1.1", 443)
PUBLIC_SITE = "example.com"
API_PORT = 8098      # the controller's API port: its replies are the one admitted inbound flow
LOCAL_PORT = 8090    # a root-owned local service no confined user may reach


def _stun_ip() -> str:
    return next(i[4][0] for i in socket.getaddrinfo("stun.l.google.com", 19302, socket.AF_INET))


@pytest.fixture(scope="module")
def local_listener():
    """A root-owned listener on [::]:LOCAL_PORT standing in for a local service."""
    srv = socket.create_server(("::", LOCAL_PORT), family=socket.AF_INET6, dualstack_ipv6=True)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield
    stop.set()
    srv.close()


@pytest.fixture(scope="module")
def controls(local_listener):
    """Root (unconfined) reaches every destination the confined users are refused."""
    stun = _stun_ip()
    assert probe(None, "tcp", TARGET_HOST, TARGET_PORT) == "connected", "private target unreachable"
    assert probe(None, "tcp", *PUBLIC) == "connected", "no public internet"
    assert probe(None, "tcp", "127.0.0.1", LOCAL_PORT) == "connected"
    assert probe(None, "tcp", "::1", LOCAL_PORT) == "connected"
    # The resolver's address with another port than DNS: reachable, so a refusal is the rule's.
    assert probe(None, "tcp", "127.0.0.11", LOCAL_PORT) == "connected"
    assert probe(None, "dns", PUBLIC_SITE, 443) == "resolved"
    assert probe(None, "udp", stun, 19302) == "answered", "STUN control failed"
    return {"stun": stun}


def _confined_denials(user, controls):
    return {
        "public tcp": probe(user, "tcp", *PUBLIC),
        "private target": probe(user, "tcp", TARGET_HOST, TARGET_PORT),
        "loopback service": probe(user, "tcp", "127.0.0.1", LOCAL_PORT),
        "ipv6 loopback": probe(user, "tcp", "::1", LOCAL_PORT),
        "udp stun": probe(user, "udp", controls["stun"], 19302),
        "dns": probe(user, "dns", PUBLIC_SITE, 443),
    }


def test_floor_installed_by_the_oneshot():
    marker = Path("/run/casa-browser/netfloor.ok")
    st = marker.lstat()
    assert st.st_uid == 0 and marker.is_file()
    listed = subprocess.run(["nft", "list", "table", "inet", "casa_browser"], capture_output=True,
                            text=True, check=True).stdout
    for uid in (1100, 1101, 1102, 1103, 1104, 1105):
        assert f"meta skuid {uid} jump" in listed


# The proxy's three listeners: the browser's, the broker's, the controller's downloader's (S4-5).
LISTENERS = {"browser": 3128, "broker": 3129, "controller": 3130}


@pytest.mark.parametrize("user", ["browser", "controller"])
def test_browser_and_controller_uid_cannot_bypass(user, controls):
    """Each of the two reaches only its own proxy listener — the browser 3128, the controller's
    downloader 3130 — and through it nothing the floor refuses."""
    own = LISTENERS[user]
    for port in LISTENERS.values():   # root reaches every listener: a refusal is the rule's
        assert probe(None, "tcp", "127.0.0.1", port) == "connected", port
    denials = _confined_denials(user, controls)
    for name, port in LISTENERS.items():
        if port != own:
            denials[f"{name} listener"] = probe(user, "tcp", "127.0.0.1", port)
    for name, verdict in denials.items():
        assert verdict.startswith("refused"), (user, name, verdict)
    assert probe(user, "tcp", "127.0.0.1", own) == "connected"
    via = {"PROBE_PROXY_PORT": str(own)}
    install_open_web(revision=1)
    try:
        assert probe(user, "https_via_proxy", PUBLIC_SITE, 443, env=via) == "status:200"
        # Through the proxy, private and loopback destinations are refused by the proxy itself.
        for host, port in [(TARGET_HOST, TARGET_PORT), ("127.0.0.1", LOCAL_PORT), ("localhost", LOCAL_PORT)]:
            verdict = probe(user, "https_via_proxy", host, port, env=via)
            assert verdict.startswith("proxy:") or verdict.startswith("status:403") or \
                verdict.startswith("refused"), (host, verdict)
    finally:
        revoke()
    # With no policy installed the proxy refuses the public site too.
    assert probe(user, "https_via_proxy", PUBLIC_SITE, 443, env=via) == "status:403"


def test_broker_uid_only_broker_listener(controls):
    denials = _confined_denials("broker", controls)
    denials["browser listener"] = probe("broker", "tcp", "127.0.0.1", 3128)
    denials["download listener"] = probe("broker", "tcp", "127.0.0.1", 3130)
    for name, verdict in denials.items():
        assert verdict.startswith("refused"), (name, verdict)
    assert probe("broker", "tcp", "127.0.0.1", 3129) == "connected"


@pytest.mark.parametrize("user", ["vnc", "ingress"])
def test_vnc_and_ingress_uids_reach_nothing(user, controls):
    """Task 13's users (INV-BRW-047): x11vnc and nginx open no connection — not the proxy, not
    the API, not a loopback service, not the network."""
    for _ in range(60):                     # the API is up (a test may have restarted it)
        if probe(None, "tcp", "127.0.0.1", API_PORT) == "connected":
            break
        time.sleep(1)
    assert probe(None, "tcp", "127.0.0.1", API_PORT) == "connected"
    denials = _confined_denials(user, controls)
    denials["browser listener"] = probe(user, "tcp", "127.0.0.1", 3128)
    denials["broker listener"] = probe(user, "tcp", "127.0.0.1", 3129)
    denials["download listener"] = probe(user, "tcp", "127.0.0.1", 3130)
    denials["api"] = probe(user, "tcp", "127.0.0.1", API_PORT)
    for name, verdict in denials.items():
        assert verdict.startswith("refused"), (user, name, verdict)
    assert len(denials) == 10


def test_proxy_uid_reaches_public_tcp_and_the_resolver_only(controls):
    assert probe("proxy", "tcp", *PUBLIC) == "connected"
    assert probe("proxy", "dns", PUBLIC_SITE, 443) == "resolved"
    for name, verdict in {
        "private target": probe("proxy", "tcp", TARGET_HOST, TARGET_PORT),
        "loopback service": probe("proxy", "tcp", "127.0.0.1", LOCAL_PORT),
        "ipv6 loopback": probe("proxy", "tcp", "::1", LOCAL_PORT),
        "udp stun": probe("proxy", "udp", controls["stun"], 19302),
        "resolver on another port": probe("proxy", "tcp", "127.0.0.11", LOCAL_PORT),
    }.items():
        assert verdict.startswith("refused"), (name, verdict)


def test_controller_api_replies_are_admitted():
    """The controller service (uid 1103) answers connections to its API port: replies are the
    one admitted inbound flow (here the /v1 guard's typed refusal of an unauthenticated call)."""
    import time
    own_ip = socket.gethostbyname(socket.gethostname())
    deadline = time.monotonic() + 60
    while True:
        try:
            c = socket.create_connection((own_ip, API_PORT), timeout=2)
            break
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail("the controller's API did not come up")
            time.sleep(0.5)
    try:
        c.sendall(b"GET /v1/hello HTTP/1.1\r\nHost: casa-browser\r\nConnection: close\r\n\r\n")
        reply = b""
        while chunk := c.recv(4096):
            reply += chunk
    finally:
        c.close()
    assert reply.startswith(b"HTTP/1.1 401"), reply[:60]
    assert b'{"error": "unauthorized"}' in reply
    # The listener belongs to the controller user (uid column of the kernel's socket table).
    owners = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in Path(table).read_text().splitlines()[1:]:
            parts = line.split()
            if parts[1].endswith(f":{API_PORT:04X}") and parts[3] == "0A":
                owners.add(int(parts[7]))
    assert owners == {1103}


def test_floor_failure_prevents_browser_start():
    """Without NET_ADMIN the install fails and the marker the launcher requires is gone."""
    marker = Path("/run/casa-browser/netfloor.ok")
    assert marker.exists()
    try:
        r = subprocess.run(["setpriv", "--bounding-set=-net_admin", "--inh-caps=-net_admin",
                            PY, "-m", "casa_browser.netfloor", "install"],
                           capture_output=True, text=True, env={"PATH": "/usr/sbin:/usr/bin:/bin",
                                                                "PYTHONPATH": "/opt"})
        assert r.returncode == 1
        assert "not installed" in r.stderr
        assert not marker.exists()
        from casa_browser import netfloor
        assert netfloor.floor_installed() is False
    finally:
        subprocess.run([PY, "-m", "casa_browser.netfloor", "install"], check=True,
                       env={"PATH": "/usr/sbin:/usr/bin:/bin", "PYTHONPATH": "/opt"})
    assert marker.exists()
