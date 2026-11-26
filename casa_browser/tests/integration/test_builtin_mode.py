"""The built image started with no 1Password token: site logins are in the app's own store.

Runs inside the app container that ``run-integration.sh`` starts with ``CASA_IT_STORE=builtin``:
the image's own s6 /init, options without a token, and a random key for the store given from
outside as ``CASA_BROWSER_VAULT_KEY``. Every fact here is one of that container: the policy
root bootstrap wrote and Chrome read, the profile Chrome made, the store the production broker
opened, where the key went, and what the production proxy answers.

The lane's other files need a container started with a token, and this one a container
started without: the script runs either this file or the others, never both.
"""
from __future__ import annotations

import html
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

import proxyharness as h
from browserprobe import ONEPASSWORD_EXTENSION, run_scenario
from probes import PY, USERS, as_user, install_policy, probe, revoke

from casa_browser.i18n import Text
from casa_browser.options import STORE_KEY_ENV

pytestmark = pytest.mark.docker

POLICY = Path("/etc/opt/chrome/policies/managed/casa.json")
SHIPPED_POLICY = Path("/opt/casa_browser/chrome_policy.json")
EXTENSION_KEYS = {"ExtensionSettings", "DeveloperToolsAvailability"}
VAULT = Path("/data/broker/vault")
BROKER_FILE = Path("/run/casa-browser/broker/broker.json")
TEAM_HOST = "casa-it.1password.eu"   # the guest address in the lane's options: a made-up team
# Longer than a forced extension takes to appear in a new profile (the 1Password lane's
# test_policy_extension_installed_after_new_profile finds it well inside this).
EXTENSION_WAIT_S = 60


def test_the_policy_chrome_reads_has_no_extension():
    """The file Chrome reads is the shipped policy without the two keys that exist for the
    extension, and Chrome's own policy page shows the policy it read: the rest in force, the
    extension's two absent."""
    shipped = json.loads(SHIPPED_POLICY.read_text())
    assert EXTENSION_KEYS <= set(shipped)        # the shipped policy does force the extension
    written = json.loads(POLICY.read_text())
    assert written == {key: value for key, value in shipped.items() if key not in EXTENSION_KEYS}
    rows = run_scenario([{"open": {"site_id": None, "mode": "open_web"}}, {"policy_rows": True}])[1]
    assert "BrowserSignin 0 Platform Machine Mandatory OK" in rows     # Chrome read the file
    assert "PasswordManagerEnabled false Platform Machine Mandatory OK" in rows
    named = {row.split(" ", 1)[0] for row in rows}
    assert not named & EXTENSION_KEYS, sorted(named)


def test_no_extension_installs_in_a_new_profile():
    """A new profile gets no 1Password extension, however long Chrome is given, and Chrome
    works under this policy: a page loads through the production proxy and code runs in it
    (the real example.com; needs the internet)."""
    profile = Path("/data/profiles/own-store")
    subprocess.run(["rm", "-rf", str(profile)], check=True)
    site = h.site("own-store", origins=("https://example.com",), login=())
    try:
        install_policy(h.account_policy(sites=(site,), run_id="it", revision=1))
        result = run_scenario([{"open": {"site_id": "own-store"}},
                               {"extension": EXTENSION_WAIT_S},
                               {"goto": "https://example.com/"},
                               {"eval": "() => location.hostname"}],
                              timeout=300)
    finally:
        revoke()
    assert result[1] is None, result[1]
    assert result[2] == 200 and result[3] == "example.com", result[2:]
    assert profile.is_dir()                       # Chrome did make the profile
    assert not list(profile.rglob(ONEPASSWORD_EXTENSION))


def _broker_status() -> dict:
    """The production broker's ``status``, asked over its socket as the controller's user."""
    code = ("import asyncio, json\nfrom casa_browser.broker_client import BrokerClient\n"
            "print('STATUS ' + json.dumps(asyncio.run(BrokerClient().status())))")
    for _ in range(60):
        r = as_user("controller", code)
        lines = [line for line in r.stdout.splitlines() if line.startswith("STATUS ")]
        reply = json.loads(lines[-1][len("STATUS "):]) if lines else {"error": "no answer"}
        if "error" not in reply:
            return reply
        time.sleep(1)                             # the broker may still be starting
    return reply


def test_the_store_is_the_brokers_and_private():
    """The production broker opened its own store, and what it made on disk is its alone: the
    directory, the file of logins, and no key file beside it, since the key came from outside."""
    assert _broker_status() == {"configured": True, "store": "builtin", "available": True}
    broker = pwd.getpwnam(USERS["broker"])
    directory = VAULT.lstat()
    assert stat.S_ISDIR(directory.st_mode) and stat.S_IMODE(directory.st_mode) == 0o700
    assert (directory.st_uid, directory.st_gid) == (broker.pw_uid, broker.pw_gid)
    logins = (VAULT / "casa.kdbx").lstat()
    assert stat.S_ISREG(logins.st_mode) and stat.S_IMODE(logins.st_mode) == 0o600
    assert logins.st_uid == broker.pw_uid
    assert not (VAULT / "key").exists()


# How Supervisor reaches the app's pages: from its ingress address, given to the container's
# loopback here, to nginx, with the headers Home Assistant sets (as ``test_ingress.py`` does).
INGRESS_PEER = ".".join(str(o) for o in (172, 30, 32, 2))
INGRESS_PORT = 8099
INGRESS_HEADERS = {"X-Ingress-Path": "/api/hassio_ingress/it-ingress-token",
                   "X-Forwarded-Host": "ha.test"}


@pytest.fixture
def ingress_peer():
    added = subprocess.run(["ip", "addr", "add", f"{INGRESS_PEER}/32", "dev", "lo"],
                           capture_output=True)
    yield INGRESS_PEER
    if added.returncode == 0:
        subprocess.run(["ip", "addr", "del", f"{INGRESS_PEER}/32", "dev", "lo"],
                       capture_output=True)


def _ingress_get(path: str) -> tuple[int, bytes]:
    """One GET through nginx from Supervisor's address: the status and the body."""
    with socket.create_connection((INGRESS_PEER, INGRESS_PORT), timeout=10,
                                  source_address=(INGRESS_PEER, 0)) as s:
        head = "".join(f"{k}: {v}\r\n" for k, v in INGRESS_HEADERS.items())
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: ha.test\r\n{head}Connection: close\r\n\r\n"
                  .encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    top, _, body = data.partition(b"\r\n\r\n")
    return int(top.split(b" ")[1]), body


def test_the_setup_page_shows_the_apps_own_store_and_no_guest(ingress_peer):
    """The running app's Setup page, asked through nginx as Supervisor asks it: the controller
    the production service started was given the app's own store, so the page's first row is
    that store, opened, and there is no 1Password row and no guest section."""
    assert _broker_status()["available"] is True          # the row below asks the broker
    for _ in range(60):
        status, body = _ingress_get("/setup")
        if status == 200:
            break
        time.sleep(1)                             # the controller may still be starting
    assert status == 200, body[:300]
    page = body.decode()
    language = re.search(r'<html lang="([^"]+)">', page).group(1)
    t = Text(language)
    shown = {key: html.escape(t(key), quote=True) in page
             for key in ("setup.store_builtin", "setup.store_builtin_unavailable",
                         "setup.store_builtin_scope", "setup.onepassword_configured",
                         "setup.onepassword_missing", "setup.guest_heading", "setup.guest_scope")}
    assert shown == {"setup.store_builtin": True, "setup.store_builtin_unavailable": False,
                     "setup.store_builtin_scope": True, "setup.onepassword_configured": False,
                     "setup.onepassword_missing": False, "setup.guest_heading": False,
                     "setup.guest_scope": False}, (language, shown)


# Chrome's own check for an update of the 1Password extension, as measured (the request the
# unit rule test sends): through the browser listener, as the browser's user, with the app's CA.
# Run after a line that sets HOST and PATH.
_EXTENSION_UPDATE = textwrap.dedent(r'''
    import socket, ssl, sys
    host, path = HOST, PATH
    s = socket.create_connection(("127.0.0.1", 3128), timeout=10)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    head = s.recv(4096).split(b"\r\n")[0]
    if b" 200" not in head:
        print("CONNECT", head.decode(errors="replace"))
        sys.exit(0)
    t = ssl.create_default_context(cafile="/run/casa-browser/ca/ca.pem").wrap_socket(
        s, server_hostname=host)
    t.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nSec-Fetch-Site: none\r\n"
               "Sec-Fetch-Mode: no-cors\r\nSec-Fetch-Dest: empty\r\nConnection: close\r\n\r\n")
              .encode())
    print("STATUS", t.recv(64).split(b"\r\n")[0].decode(errors="replace").split(" ")[1])
''')
EXTENSION_UPDATE = (
    "clients2.google.com",
    "/service/update2/crx?os=linux&arch=x86_64&prod=chromecrx&prodchannel="
    "&prodversion=155.0.8059.84&lang=en-GB&acceptformat=crx3,puff"
    "&x=id%3Daeblfdkhhhdcdjpifhhbdiojplfjncoa%26installsource%3Dpolicy%26uc")


def _proxy_events() -> list[list]:
    """The production proxy's request events, as the controller reads them."""
    r = as_user("controller", "import asyncio, json\nfrom casa_browser.proxy_client import ProxyClient\n"
                "ev, _ = asyncio.run(ProxyClient().events(0))\n"
                "print('EVENTS', json.dumps([[e.seq, e.listener, e.origin, e.allow, e.reason] for e in ev]))")
    assert r.returncode == 0, r.stderr[-400:]
    return json.loads(next(line for line in r.stdout.splitlines()
                           if line.startswith("EVENTS "))[len("EVENTS "):])


def test_the_browser_listener_refuses_chromes_extension_update():
    """The production proxy was started for the app's own store: Chrome's update check for the
    1Password extension, sent as Chrome sends it through the browser listener while an account
    run's policy is in force, is refused as Chrome's own background traffic and nothing of it
    is admitted. Under 1Password the same request is admitted, so the extension can install
    (``test_browser_runtime.py``)."""
    host, path = EXTENSION_UPDATE
    site = h.site("own-store-update", origins=("https://example.com",), login=())
    try:
        install_policy(h.account_policy(sites=(site,), run_id="it", revision=1))
        # Events keep their origin only once the proxy's credential window is closed.
        r = as_user("controller", "import asyncio\nfrom casa_browser.proxy_client import ProxyClient\n"
                    "async def main():\n    c = ProxyClient()\n    await c.close_window(await c.open_window())\n"
                    "    for _ in range(300):\n        if not (await c.status())['window_open']:\n            return\n"
                    "        await asyncio.sleep(0.1)\n    raise SystemExit('the window stayed open')\n"
                    "asyncio.run(main())")
        assert r.returncode == 0, r.stderr[-400:]
        after = max([e[0] for e in _proxy_events()], default=0)
        sent = as_user("browser", f"HOST, PATH = {host!r}, {path!r}\n" + _EXTENSION_UPDATE)
        assert sent.returncode == 0, sent.stderr[-400:]
        assert sent.stdout.split() == ["STATUS", "403"], sent.stdout
        events = [e for e in _proxy_events() if e[0] > after]
    finally:
        revoke()
    asked = [e[1:] for e in events if e[2] == "https://" + host]
    assert asked == [["browser", "https://" + host, False, "browser_background"]], events
    assert [e for e in events if e[3]] == [], events


# Run as a service user with the key on stdin (never in an argument): one verdict per process,
# each line its pid and the verdict.
_ENVIRONMENTS = textwrap.dedent('''
    import sys
    key = sys.stdin.buffer.read()
    for pid in sys.argv[2:]:
        try:
            data = open(f"/proc/{pid}/environ", "rb").read()
        except OSError:
            print(pid, "unreadable")
            continue
        named = any(entry.startswith(sys.argv[1].encode() + b"=") for entry in data.split(b"\\0"))
        print(pid, "leak" if named or key in data else ("clean" if b"PATH=" in data else "empty"))
''')
# How often a user's processes are listed again when one of them ended while it was read.
_RELISTS = 5


def _environments(user: str, pids: list[str], key: str) -> dict[str, str]:
    name = USERS[user]
    r = subprocess.run(["setpriv", f"--reuid={name}", f"--regid={name}", "--clear-groups",
                        "--inh-caps=-all", "--bounding-set=-all", PY, "-c", _ENVIRONMENTS,
                        STORE_KEY_ENV, *pids],
                       input=key.encode(), capture_output=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr[-400:]
    verdicts = dict(line.split() for line in r.stdout.decode().splitlines())
    assert sorted(verdicts) == sorted(pids), (user, sorted(verdicts), sorted(pids))
    return verdicts


def _read_every_process(user: str, key: str) -> dict[str, str]:
    """The verdict on the environment of every process the service user runs, each read as
    that user. A process that could not be read because it ended in the meantime is no
    verdict: the user's processes are listed and read again. One that could not be read and is
    still running is returned as ``unreadable``."""
    for _ in range(_RELISTS):
        listed = subprocess.run(["pgrep", "-u", USERS[user]], capture_output=True, text=True)
        pids = listed.stdout.split()
        if not pids:
            return {}
        verdicts = _environments(user, pids, key)
        unread = [pid for pid, verdict in verdicts.items() if verdict == "unreadable"]
        if not unread or any(Path(f"/proc/{pid}").exists() for pid in unread):
            return verdicts
        # every process that could not be read has ended: list them again
    raise AssertionError(f"the processes of {user} kept ending while they were read")


def test_the_key_variable_reaches_no_service():
    """The store's key is set on the container, as an operator sets it. Root bootstrap hands it
    to the broker in the broker's own file and removes it from the environment services start
    from: no service process has it, and no other file holds it. Nothing here prints it.

    Every running process of every service user is read, as that user: each one's environment
    has to be readable and hold neither the variable nor the key. A process that ended while
    it was read is read again from a new listing; one still running whose environment cannot
    be read fails the test, since it could be the one holding the key."""
    key = os.environ.get(STORE_KEY_ENV, "")
    set_by_the_lane = len(key) >= 32
    assert set_by_the_lane, "the lane did not set the store's key on the container"
    env_dir = Path("/run/s6/container_environment")
    assert (env_dir / "PATH").is_file()           # s6 did write the container's environment here
    assert not (env_dir / STORE_KEY_ENV).exists()
    assert [p.name for p in env_dir.iterdir() if p.is_file() and key.encode() in p.read_bytes()] == []
    read = {}
    for user in USERS:
        verdicts = _read_every_process(user, key)
        not_clean = {pid: v for pid, v in verdicts.items() if v not in ("clean", "empty")}
        assert not_clean == {}, (user, not_clean)
        if verdicts:
            read[user] = len(verdicts)
    # the control: these services run, and every one of their processes was read
    assert {"broker", "controller", "proxy"} <= set(read), read
    holders = sorted(str(f) for root in ("/data", "/run") for f in Path(root).rglob("*")
                     if f.is_file() and not f.is_symlink() and key.encode() in f.read_bytes())
    assert holders == [str(BROKER_FILE)]
    in_the_brokers_file = json.loads(BROKER_FILE.read_text())["store_key"] == key
    assert in_the_brokers_file


def test_the_broker_listener_refuses_the_team_host():
    """The lane's options name the same team address as the guest address as they do when a
    token is set, where the proxy's broker listener grants the broker user a tunnel to it
    (``test_network_floor.py``). Started without a token the proxy refuses that very request:
    no 1Password host has special access. The refusal is the proxy's own answer, so the
    listener was reached."""
    options = json.loads(Path("/data/options.json").read_text())
    assert options["guest_sign_in_address"] == TEAM_HOST
    assert not options.get("onepassword_service_account_token")
    verdict = probe("broker", "connect_via_proxy", TEAM_HOST, 443,
                    env={"PROBE_PROXY_PORT": "3129"})
    assert verdict.startswith("proxy:HTTP/1.1 403"), verdict
