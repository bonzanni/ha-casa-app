"""Task 14 Step 2 in the production container: who can read what, what listens, where the
Supervisor token went, and a synthetic HA backup exported and restored.

* every service user reads only its own private files — the options, the broker's
  credentials and state, the proxy's CA key and binding key, the controller's store, the
  browser's profiles — and connects only to the sockets meant for it;
* the Supervisor token (a canary here, set on the container as Supervisor sets the real one)
  is gone from the s6 container environment and from every service's environment, no service
  user can read init's environment, and no file under /data or /run holds it;
* nothing listens beyond the API (8098), nginx (8099) and the two proxy listeners on
  loopback — not while Chrome runs either (its only debugging channel is a pipe);
* the proxy down, the browser user still reaches nothing (no failover to a direct route);
* a backup made as Supervisor makes it (``backup_exclude``) holds no profile, no download
  and no proxy key, and after a restore the registry and the run records are back, the site's profile is gone (it needs a login again) and its provider binding, learned
  under the old proxy key, needs a live login.

This module runs last in the lane: the restore rewrites /data (to the backup's content).

**Every refusal or absence assertion has a positive control on the same target** (operator
ruling E-T14-1 = O1): the owner reads every private file another user cannot; the controller
is answered on the sockets others are not; the token is in the test process's environment
and a planted binary canary is found by the file scan before it reports none; each user's own
service environment is read; Chrome runs while nothing unexpected listens; root and the
browser user reach the targets (and the proxy's port) just before the proxy stops; the
excluded places hold content before the backup leaves them out.

**Known test limits** (operator ruling E-T14-2 = P3: documented, not fixed): the listener scan
has no control that it sees a listener at all; one readable process environment of a user
stands in for that user's other processes (and a user with no process is not checked);
``/data/options.json`` has no root read control; the s6 container-environment check has no
planted control; the browser's refusal on port 3129 has no reachability control before the
proxy stops; the container-log scan in ``run-integration.sh`` plants its control on stdout
only (Docker's log carries stdout and stderr together).
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
import time
from pathlib import Path, PurePath

import pytest

from lifecycleprobe import run_lifecycle
from probes import PY, USERS, as_user, probe
from test_run_lifecycle import _chrome_processes, fixture_config

pytestmark = pytest.mark.docker

CANARY = os.environ.get("CASA_IT_SUPERVISOR_CANARY", "")
SERVICES = ("svc-browser-controller", "svc-browser-broker", "svc-browser-proxy")
BACKUP_EXCLUDE = ("profiles/*", "downloads/*", "proxy/*")   # casa_browser/config.yaml


def _connect(user: str, path: str) -> str:
    r = subprocess.run(["setpriv", f"--reuid={USERS[user]}", f"--regid={USERS[user]}",
                        "--init-groups", "--inh-caps=-all", "--bounding-set=-all", PY, "-c",
                        "import socket,sys\ns=socket.socket(socket.AF_UNIX)\ns.settimeout(5)\n"
                        "try:\n    s.connect(sys.argv[1]); print('connected')\n"
                        "except OSError:\n    print('refused')", path],
                       capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    return r.stdout.strip()


def _first(pattern: str) -> str | None:
    hits = sorted(Path("/").glob(pattern.lstrip("/")))
    return str(hits[0]) if hits else None


# The private files and who alone may read them (the store is the controller's; options.json
# is root's only).
PRIVATE = {
    "/data/options.json": set(),
    "/run/casa-browser/broker/broker.json": {"broker"},
    "/data/broker": {"broker"},
    "/run/casa-browser/proxy-private": {"proxy"},
    "/data/proxy/binding.key": {"proxy"},
    "/data/state": {"controller"},
    "/data/profiles": {"browser"},
}


def _children(path: str) -> list[str]:
    """Every regular file inside a private place (as root); the file itself for a file."""
    p = Path(path)
    if p.is_file():
        return [path]
    return sorted(str(f) for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def _readable(user: str, files: list[str]) -> set[str]:
    """The files ``user`` can open and read a byte of (one process per user, so every file of
    a large profile is probed)."""
    import json
    r = subprocess.run(["setpriv", f"--reuid={USERS[user]}", f"--regid={USERS[user]}",
                        "--init-groups", "--inh-caps=-all", "--bounding-set=-all", PY, "-c",
                        "import json,sys\nok=[]\n"
                        "for p in json.load(sys.stdin):\n"
                        "    try:\n        open(p,'rb').read(1); ok.append(p)\n"
                        "    except OSError:\n        pass\n"
                        "print(json.dumps(ok))"],
                       input=json.dumps(files), capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr[-400:]
    return set(json.loads(r.stdout))


@pytest.mark.parametrize("path", sorted(PRIVATE))
def test_each_private_file_reads_only_as_its_owner(path):
    """Every regular file in each private place — all of them — is read by its owner (the
    positive control) and by no other service user."""
    assert Path(path).exists(), path
    files = _children(path)
    assert files, f"nothing to probe in {path}"
    for user in USERS:
        readable = _readable(user, files)
        if user in PRIVATE[path]:
            assert readable == set(files), (user, sorted(set(files) - readable)[:5])
        else:
            assert readable == set(), (user, sorted(readable)[:5])


def _status_reply(user: str, path: str) -> str:
    r = subprocess.run(
        ["setpriv", f"--reuid={USERS[user]}", f"--regid={USERS[user]}", "--init-groups",
         "--inh-caps=-all", "--bounding-set=-all", PY, "-c",
         "import socket,sys,json\ns=socket.socket(socket.AF_UNIX)\ns.settimeout(5)\n"
         "try:\n    s.connect(sys.argv[1])\nexcept OSError:\n    print('refused'); raise SystemExit\n"
         # A refused peer's connection can be closed by the server before the request is
         # written; sendall then meets the closed end (measured under load: EPIPE). That is the
         # refusal, not a crash. Only those two errors are a verdict; any other ends as crashed.
         "try:\n    s.sendall(json.dumps({'op': 'status'}).encode() + b'\\n')\n"
         "except (BrokenPipeError, ConnectionResetError):\n    print('unanswered'); raise SystemExit\n"
         "try:\n    d = s.recv(65536)\nexcept OSError:\n    d = b''\n"
         "print('answered' if b'\"ok\": true' in d else 'unanswered')", path],
        capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    return r.stdout.strip() or "crashed:" + r.stderr[-200:]


def test_control_sockets_admit_only_their_peer():
    """The proxy's control socket answers the controller only (its peer check); the broker's RPC
    socket admits the controller only (its group); nobody else gets a connection answered."""
    proxy_sock = "/run/casa-browser/proxy/control.sock"
    broker_sock = "/run/casa-browser/broker/rpc.sock"
    for user in USERS:
        verdict = _status_reply(user, proxy_sock)
        assert verdict == ("answered" if user == "controller" else verdict), (user, verdict)
        if user != "controller":
            assert verdict in ("refused", "unanswered"), (user, verdict)
        verdict = _connect(user, broker_sock)
        if user == "controller":   # the positive control: its one client connects
            assert verdict == "connected", (user, verdict)
        elif user != "broker":     # the broker serves it
            assert verdict == "refused", (user, verdict)


def test_supervisor_token_reaches_no_service():
    assert CANARY.startswith("it-supervisor-"), "the lane did not set the token canary"
    assert os.environ.get("SUPERVISOR_TOKEN") == CANARY   # positive control: Docker set it
    env_dir = Path("/run/s6/container_environment")
    assert not (env_dir / "SUPERVISOR_TOKEN").exists() and not (env_dir / "HASSIO_TOKEN").exists()
    assert not any(CANARY in p.read_text(errors="ignore") for p in env_dir.iterdir() if p.is_file())
    # every service's own environment, read as that service's user (root may not ptrace them)
    for user, name in USERS.items():
        pids = subprocess.run(["pgrep", "-u", name], capture_output=True, text=True).stdout.split()
        verdicts = []
        for pid in pids:
            r = as_user(user, f"try:\n    d=open('/proc/{pid}/environ','rb').read()\n"
                              f"except OSError:\n    print('unreadable'); raise SystemExit\n"
                              f"print('leak' if {CANARY!r}.encode() in d else ('clean' if b'PATH=' in d else 'empty'))")
            verdicts.append(r.stdout.strip())
        assert "leak" not in verdicts, (user, verdicts)
        if pids:   # positive control: the user's own service environment was actually read
            # (nginx empties its own environment, so an empty one is a read, too)
            assert {"clean", "empty"} & set(verdicts), (user, verdicts)
        # init's environment (which Docker set) is not readable by a service user
        r = as_user(user, "try:\n    open('/proc/1/environ','rb').read(); print('read')\n"
                          "except OSError:\n    print('denied')")
        assert r.stdout.strip() == "denied", (user, r.stdout)
    # and no file under /data or /run holds it — binary files (databases) included; the scan
    # finds a planted binary canary first (its positive control)
    planted = Path("/data/it-token-canary.bin")
    planted.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64 + CANARY.encode() + b"\x00\xff")
    try:
        assert _files_containing(CANARY.encode()) == [str(planted)]
    finally:
        planted.unlink()
    assert _files_containing(CANARY.encode()) == []


def _files_containing(needle: bytes, roots=("/data", "/run/casa-browser")) -> list[str]:
    """Every regular file under ``roots`` whose bytes hold ``needle`` (root reads all; an
    unreadable file fails the scan rather than being skipped)."""
    hits = []
    for root in roots:
        for f in Path(root).rglob("*"):
            if f.is_symlink() or not f.is_file():
                continue
            if needle in f.read_bytes():
                hits.append(str(f))
    return sorted(hits)


def _listeners() -> set[tuple[str, int]]:
    out = set()
    for table, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        for line in Path(table).read_text().splitlines()[1:]:
            parts = line.split()
            if parts[3] != "0A":
                continue
            addr, port = parts[1].split(":")
            out.add(("v6:" + addr if v6 else addr, int(port, 16)))
    return out


LOOPBACK4 = "0100007F"
# Docker's embedded DNS resolver listens on 127.0.0.11 inside the container's network
# namespace (it is Docker's, reached through Docker's own NAT; HA apps get the same resolver).
DOCKER_DNS = "0B00007F"


def _unexpected(listeners):
    allowed_any = {8098, 8099}
    allowed_loop = {3128, 3129, 3130}     # the proxy's browser, broker and download listeners
    bad = []
    for addr, port in listeners:
        if port in allowed_any:
            continue
        if port in allowed_loop and addr == LOOPBACK4:
            continue
        if addr == DOCKER_DNS:
            continue
        bad.append((addr, port))
    return bad


def test_nothing_listens_beyond_the_api_nginx_and_the_proxy(tmp_path):
    """Also while Chrome runs for a run: no debugging port, no VNC port."""
    from lifecycleprobe import controller_home
    assert not _unexpected(_listeners()), _listeners()
    seen = []

    def check(proc, line):
        seen.append((_listeners(), bool(_chrome_processes())))

    steps = [{"run": {"name": "r", "request_id": "it-listen", "mode": "open_web"}},
             {"wait_state": {"name": "r", "state": "running"}},
             {"observe": {"name": "r"}},
             {"ready": {"hold": 1}},
             {"cancel": {"name": "r"}}]
    run_lifecycle(steps, home=controller_home(tmp_path), on_ready=check)
    assert seen and seen[0][1], "Chrome was not running during the check"
    assert not _unexpected(seen[0][0]), seen[0][0]


def test_browser_reaches_nothing_while_the_proxy_is_down():
    """With the proxy stopped the browser user has no route: not the proxy's port (which it
    reaches while the proxy runs), not a public address and not the private target (both of
    which root reaches at that moment) — the positive controls, on the same targets."""
    target_host, target_port = os.environ["CASA_IT_PRIVATE_TARGET"].rsplit(":", 1)
    targets = (("1.1.1.1", 443), (target_host, int(target_port)))
    assert probe("browser", "tcp", "127.0.0.1", 3128) == "connected"
    subprocess.run(["/command/s6-svc", "-wd", "-T", "15000", "-d", "/run/service/svc-browser-proxy"],
                   check=True)
    try:
        for host, port in targets:
            assert probe(None, "tcp", host, port) == "connected", (host, port)
        for host, port in (("127.0.0.1", 3128), ("127.0.0.1", 3129), ("127.0.0.1", 3130),
                           *targets):
            verdict = probe("browser", "tcp", host, port)
            assert verdict.startswith("refused"), (host, port, verdict)
    finally:
        subprocess.run(["/command/s6-svc", "-wU", "-T", "30000", "-u", "/run/service/svc-browser-proxy"],
                       check=True)


# --------------------------------------------------------------------------- backup / restore


def _excluded(relative: str) -> bool:
    """Supervisor's rule (casa-browser.md): each pattern is matched against the end of the
    path, glob-style; an excluded directory is not descended into."""
    return any(PurePath(relative).match(p) for p in BACKUP_EXCLUDE)


def _supervisor_backup(root: Path = Path("/data")) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(path: Path):
            rel = path.relative_to(root).as_posix()
            if rel != "." and _excluded(rel):
                return
            tar.add(path, arcname=rel, recursive=False)
            if path.is_dir() and not path.is_symlink():
                for child in sorted(path.iterdir()):
                    add(child)
        add(root)
    return buf.getvalue()


def _services(action: str) -> None:
    flag = {"down": ["-wd", "-T", "20000", "-d"], "up": ["-wU", "-T", "60000", "-u"]}[action]
    for svc in SERVICES if action == "down" else reversed(SERVICES):
        subprocess.run(["/command/s6-svc", *flag, f"/run/service/{svc}"], check=True)


def _restore(archive: bytes, root: Path = Path("/data")) -> None:
    for child in root.iterdir():
        subprocess.run(["rm", "-rf", "--one-file-system", str(child)], check=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(root, filter="tar")
    # the app's own start after a restore: root bootstrap, then the services
    subprocess.run(["/etc/s6-overlay/scripts/browser-config.sh"], check=True,
                   env={"PATH": "/command:/usr/bin:/bin"})


def test_backup_excludes_profiles_downloads_and_the_proxy_key_and_restore_needs_logins(tmp_path):
    home = Path("/data/it-home")          # the scenario's store lives in /data: it is backed up
    home.mkdir(exist_ok=True)
    os.chown(home, 1103, 1103)
    os.chmod("/data", 0o755)
    first = [
        {"site": {"site_id": "sa", "name": "Site A", "origins": ["https://app.site-a.example"],
                  "login": ["https://login.site-a.example"], "item": None,
                  "start": "https://app.site-a.example/home"}},
        {"bind_provider": {"site_id": "sa"}},
        {"bindings_lost": None},                                        # 2
        {"run": {"name": "r", "request_id": "it-backup", "requester": "bob",
                 "sites": [["sa", "account"]]}},
        {"wait_state": {"name": "r", "state": ["running", "interrupted", "failed"], "timeout": 90}},
        {"cancel": {"name": "r"}},
        {"run": {"name": "w", "request_id": "it-backup-web", "requester": "bob", "mode": "open_web"}},
        {"wait_state": {"name": "w", "state": "running"}},
        {"finish": {"name": "w"}},
        {"records": {"requester": "bob"}},                              # 9
    ]
    out = run_lifecycle(first, home=home, broker_fixture=fixture_config(tmp_path))
    assert out[2] == [], out[2]
    assert len(out[9]) >= 1, out[9]
    records_before = len(out[9])
    profiles = [p.name for p in Path("/data/profiles").iterdir()]
    assert "sa" in profiles, profiles                    # the account run made its profile
    marker = Path("/data/downloads/partial.crdownload")
    marker.write_bytes(b"partial")
    assert Path("/data/proxy/binding.key").exists()
    old_key = Path("/data/proxy/binding.key").read_bytes()

    _services("down")
    try:
        archive = _supervisor_backup()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = tar.getnames()
        for excluded in ("profiles/", "downloads/", "proxy/"):
            assert not [n for n in names if n.startswith(excluded)], (excluded, names)
        for kept in ("options.json", "state", "broker", "it-home/state.db"):
            assert kept in names, (kept, names)
        _restore(archive)
    finally:
        _services("up")
    _wait_proxy()

    assert not any(Path("/data/profiles").iterdir())     # the profile is gone: a login again
    assert Path("/data/proxy/binding.key").read_bytes() != old_key   # a new key was made
    second = [
        {"sites": None},                                                # 0
        {"records": {"requester": "bob"}},                              # 1
        {"bindings_lost": None},                                        # 2
    ]
    out = run_lifecycle(second, home=home)
    assert out[0] == ["sa"] and len(out[1]) == records_before, out
    assert out[2] == ["sa"], out[2]       # the provider binding needs a live login
    subprocess.run(["rm", "-rf", str(home)], check=True)
    assert not _chrome_processes()


def _wait_proxy(timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path("/run/casa-browser/proxy/control.sock").exists() and \
                Path("/run/casa-browser/ca/ca.pem").exists():
            r = subprocess.run(["/command/s6-svstat", "-u", "/run/service/svc-browser-proxy"],
                               capture_output=True, text=True)
            if r.stdout.strip() == "true":
                time.sleep(2)
                return
        time.sleep(0.5)
    raise RuntimeError("proxy did not come back")
