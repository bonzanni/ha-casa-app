"""Probes run inside the app container as a given service user (``setpriv``), against the
production floor and proxy. Each returns a short verdict string; a probe never reports
"refused" for a destination it could not have reached anyway — every test pairs its denied
probes with a root (unconfined) positive control on the same destination."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

PY = "/opt/venv/bin/python"
USERS = {"browser": "casa-browser", "proxy": "casa-proxy", "broker": "casa-broker",
         "controller": "casa-controller", "vnc": "casa-vnc", "ingress": "casa-ingress"}
CA = "/run/casa-browser/ca/ca.pem"

_SNIPPET = textwrap.dedent(r'''
    import json, socket, ssl, sys, os, struct
    kind, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
    def out(v): print(json.dumps(v)); sys.exit(0)
    try:
        if kind == "tcp":
            fam = socket.AF_INET6 if ":" in host else socket.AF_INET
            s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(5); s.connect((host, port)); out("connected")
        if kind == "udp":  # a STUN binding request; "answered" only if a reply comes back
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
            msg = struct.pack("!HHI12s", 1, 0, 0x2112A442, os.urandom(12))
            s.sendto(msg, (host, port)); s.recvfrom(2048); out("answered")
        if kind == "dns":
            socket.getaddrinfo(host, port); out("resolved")
        if kind == "https_via_proxy":  # one GET through a proxy listener (PROBE_PROXY_PORT) with the app's CA
            proxy_port = int(os.environ.get("PROBE_PROXY_PORT", "3128"))
            s = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
            s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            head = s.recv(4096)
            if b" 200" not in head.split(b"\r\n")[0]:
                out("proxy:" + head.split(b"\r\n")[0].decode(errors="replace"))
            ctx = ssl.create_default_context(cafile="/run/casa-browser/ca/ca.pem")
            t = ctx.wrap_socket(s, server_hostname=host)
            t.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nSec-Fetch-Dest: document\r\nSec-Fetch-Mode: navigate\r\nConnection: close\r\n\r\n".encode())
            out("status:" + t.recv(64).split(b"\r\n")[0].decode(errors="replace").split(" ")[1])
        out("unknown-probe")
    except (OSError, ssl.SSLError) as e:
        out(f"refused:{type(e).__name__}:{getattr(e, 'errno', '')}")
''')


def probe(user: str | None, kind: str, host: str, port: int, env: dict | None = None) -> str:
    cmd = [PY, "-c", _SNIPPET, kind, host, str(port)]
    if user is not None:
        name = USERS[user]
        cmd = ["setpriv", f"--reuid={name}", f"--regid={name}", "--clear-groups",
               "--inh-caps=-all", "--bounding-set=-all", *cmd]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt", **(env or {})})
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return f"crashed:{r.returncode}:{r.stderr[-200:]}"


def as_user(user: str, code: str) -> subprocess.CompletedProcess:
    name = USERS[user]
    return subprocess.run(["setpriv", f"--reuid={name}", f"--regid={name}", "--clear-groups",
                           "--inh-caps=-all", "--bounding-set=-all", PY, "-c", code],
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt"})


def install_open_web(revision: int = 1) -> None:
    """Install an open-web policy as the controller user (the only uid the control socket admits)."""
    code = textwrap.dedent(f"""
        import asyncio
        from casa_browser.policy import RunPolicy, SessionRef
        from casa_browser.proxy_client import ProxyClient
        p = RunPolicy(run_id="it", mode="open_web", site_grants=(), sites=(),
                      login_lease=None, manual_lease=None, revision={revision},
                      active_session=SessionRef("public", None, 1))
        print(asyncio.run(ProxyClient().install(p)))
    """)
    r = as_user("controller", code)
    assert r.returncode == 0 and r.stdout.strip() == str(revision), r.stderr[-400:]


def revoke() -> None:
    r = as_user("controller", "import asyncio\nfrom casa_browser.proxy_client import ProxyClient\n"
                              "asyncio.run(ProxyClient().revoke('it'))")
    assert r.returncode == 0, r.stderr[-400:]


def install_policy(policy) -> None:
    """Install any RunPolicy into the production proxy as the controller user."""
    import base64
    from casa_browser.proxy_client import encode_policy
    blob = base64.b64encode(json.dumps(encode_policy(policy)).encode()).decode()
    code = textwrap.dedent(f"""
        import asyncio, base64, json
        from casa_browser.proxy_client import ProxyClient, decode_policy
        p = decode_policy(json.loads(base64.b64decode("{blob}")))
        print(asyncio.run(ProxyClient().install(p)))
    """)
    r = as_user("controller", code)
    assert r.returncode == 0, r.stderr[-400:]
