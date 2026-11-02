"""Task 12 in the image: the production run controller with Chrome (the browser user, through
the launcher), the enforcing proxy (the test harness on the production port, or the production
proxy itself) and, for account runs, the uid-separated test broker (fake SDK). Synthetic sites.

* a profile switch from an account site to a public grant, a public download, a cancelled
  download and an idempotent start;
* a broker login through the driver;
* a forced driver close (K1) during a login whose renderer never answers;
* a proxy restart mid-run and a controller kill and restart (production proxy).
"""
from __future__ import annotations

import asyncio
import signal
import subprocess
import time
from pathlib import Path

import pytest

from brokerfakes import canary, make_item
from lifecycleprobe import controller_home, run_lifecycle
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker

A, B, LOGIN = "site-a.example", "pub-b.example", "login.site-a.example"
PDF = b"%PDF-1.7\n% synthetic\n" + b"0" * 2000 + b"\n%%EOF\n"
PROXY_SERVICE = "/run/service/svc-browser-proxy"


def fixture_config(tmp_path):
    """The uid-separated test broker's fixture (as ``integration/test_login.py`` builds it)."""
    import json
    import os
    tmp_path.chmod(0o755)
    for p in tmp_path.parents:
        if str(p).startswith("/tmp/"):
            p.chmod(0o755)
    root = tmp_path / "broker"
    root.mkdir(mode=0o711)
    os.chown(root, 1102, 1102)
    controller = tmp_path / "controller"
    controller.mkdir(mode=0o711)
    os.chown(controller, 1103, 1103)
    snapshot = root / "fake-vault.json"
    snapshot.write_text(json.dumps([make_item().model_dump(mode="json", by_alias=True)]))
    os.chown(snapshot, 1102, 1102)
    return {"root": str(root), "driver_socket": str(controller / "driver.sock"),
            "options": {"op_token": canary(), "vault": "Synthetic vault",
                        "guest_sign_in_address": "acct.1password.eu", "guest_email": canary(),
                        "guest_password": canary(), "guest_secret_key": canary()}}


def form(role, action):
    kind = "password" if "password" in role else "text"
    return ("text/html", (f'<form method=post action="{action}"><input id=field name=value '
                          f'type={kind} autocomplete={role}><button>Continue</button></form>').encode(), {})


def _controller_uid(sites):
    sites.call(setattr, sites.harness.proxy, "controller_uid", 1103)


def _chrome_processes() -> list[str]:
    out = subprocess.run(["ps", "-o", "pid=,args=", "-u", "casa-browser"], capture_output=True,
                         text=True).stdout
    return [l for l in out.splitlines() if "chrome" in l]


def test_profile_switch_public_download_and_cancel(tmp_path):
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/start"] = ("text/html", b"<title>Account</title><p>Account home</p>",
                                 {"Set-Cookie": "acct=AcctSession123; Secure; Path=/"})
        sites.pages["/pub"] = ("text/html", (
            b"<title>Public</title><p>Public documents</p>"
            b'<a href="https://pub-b.example/doc.pdf">Doc PDF</a> '
            b'<a href="https://pub-b.example/slow.pdf">Slow PDF</a>'), {})
        sites.pages["/doc.pdf"] = ("application/pdf", PDF, {})
        sites.pages["/slow.pdf"] = ("application/pdf", PDF, {})

        def hold_responses(proc, line):
            sites.call(lambda: setattr(sites.harness.fixture, "response_hold", asyncio.Event()))

        steps = [
            {"site": {"site_id": "sa", "name": "Site A", "origins": [f"https://{A}"],
                      "start": f"https://{A}/start"}},
            {"site": {"site_id": "sp", "name": "Public B", "origins": [f"https://{B}"],
                      "start": f"https://{B}/pub"}},
            {"run": {"name": "r", "request_id": "it-1", "sites": [["sa", "account"], ["sp", "public"]]}},
            {"run": {"name": "r", "request_id": "it-1", "sites": [["sa", "account"], ["sp", "public"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "navigate", "label": "Open site: Public B"}},
            {"observe": {"name": "r"}},
            {"download": {"name": "r", "label": "Doc PDF"}},
            {"files": None},
            {"ready": {"hold": 1}},
            {"download": {"name": "r", "label": "Slow PDF", "background": True, "after": 2}},
            {"cancel": {"name": "r"}},
            {"await_download": None},
            {"files": None},
            {"events": {"name": "r"}},
            {"browser": None},
        ]
        out = run_lifecycle(steps, home=controller_home(tmp_path), broker_fixture=config,
                            on_ready=hold_responses)
        assert out[2] == {"created": True} and out[3] == {"created": False}
        assert out[4] == "running"
        assert "Account home" in out[5]["text"] and out[5]["source_id"] is None
        assert out[6]["outcome"] == "done"
        assert "Public documents" in out[7]["text"] and out[7]["source_id"]
        assert out[8]["head"] == "%PDF-" and out[8]["size"] == len(PDF)
        assert len(out[9]) == 1
        assert out[12] == "cancelled" and out[13] == {"error": "interrupted"}
        assert out[14] == out[9]                       # the cancelled transfer left nothing
        kinds = [k for k, _ in out[15]]
        assert kinds.count("run_started") == 1 and "file_saved" in kinds
        assert kinds[-1] == "run_cancelled" and out[16] is False
        # One browser at a time: the account profile's cookie never reached the public site.
        assert not [c for c in sites.receipts.cookies if c[0].startswith(B)]
        assert not _chrome_processes()


def test_broker_login_through_the_driver(tmp_path):
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, A]) as sites:
        _controller_uid(sites)
        sites.pages["/username"] = form("username", "/password")
        sites.pages["/password"] = form("current-password", "/finish")
        sites.pages["/finish"] = ("text/html", b"", {"Location": f"https://{A}/done"})
        sites.status["/finish"] = 303
        sites.pages["/done"] = ("text/html", b"<title>Home</title><p>Signed in</p>", {})
        steps = [
            {"site": {"site_id": "s1", "name": "Site A", "origins": [f"https://{A}"],
                      "login": [f"https://{LOGIN}"], "item": "item",
                      "start": f"https://{LOGIN}/username"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{LOGIN}"], "operator_ref": "op"}},
            {"broker": {"op": "status"}},   # was the guest confirmation (gone, slice 7)
            {"run": {"name": "r", "request_id": "it-login", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 3.5},     # the credential window's quiet interval after close_window
            {"observe": {"name": "r"}},
            {"events": {"name": "r"}},
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=controller_home(tmp_path), broker_fixture=config)
        assert out[4] == "running"
        assert "Signed in" in out[6]["text"], out
        posts = [r for r in sites.receipts.requests if r[0] == "POST"]
        assert [p[2] for p in posts] == ["/password", "/finish"]
        needed = [o for k, o in out[7] if k == "login_needed"]
        # no extension in the image, so the guest is not unlocked: that asks for nothing (5s)
        assert needed == [], needed
        assert out[8] == "completed"


def test_forced_driver_close_during_login(tmp_path):
    """A login whose renderer stops answering is cut by cancel: the driver op does not drain
    within its bound, so the driver kills Chrome (K1); nothing is filled, the run ends, no
    browser is left."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, A]) as sites:
        _controller_uid(sites)
        sites.pages["/busy"] = ("text/html", b"<title>Sign in</title><p>Sign in</p>", {})
        steps = [
            {"site": {"site_id": "s1", "name": "Site A", "origins": [f"https://{A}"],
                      "login": [f"https://{LOGIN}"], "item": "item",
                      "start": f"https://{LOGIN}/busy"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{LOGIN}"], "operator_ref": "op"}},
            {"broker": {"op": "status"}},   # was the guest confirmation (gone, slice 7)
            {"stall_login": None},
            {"run": {"name": "r", "request_id": "it-k1", "sites": [["s1", "account"]]}},
            {"wait_stall": 150},   # past the guest, on the start page: the broker's hung op
            {"clock": None},
            {"cancel": {"name": "r"}},
            {"clock": None},
            {"state": {"name": "r"}},
            {"browser": None},
            {"events": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=controller_home(tmp_path), broker_fixture=config,
                            timeout=240)
        assert out[7] == out[9] == "cancelled" and out[10] is False
        assert out[8] - out[6] >= 9      # the cleanup waited out its 10 s drain bound: forced
        assert "login_needed" not in [k for k, _ in out[11] if _ != "guest_prompt"]
        assert not [r for r in sites.receipts.requests if r[0] == "POST"]
        assert not _chrome_processes()


def _wait_proxy_back(timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path("/run/casa-browser/proxy/control.sock").exists():
            r = subprocess.run(["/command/s6-svstat", "-u", PROXY_SERVICE], capture_output=True,
                               text=True)
            if r.stdout.strip() == "true":
                return
        time.sleep(0.5)
    raise RuntimeError("proxy did not come back")


def test_proxy_restart_interrupts_the_run(tmp_path):
    def restart(proc, line):
        subprocess.run(["/command/s6-svc", "-r", PROXY_SERVICE], check=True)
        time.sleep(1)
        _wait_proxy_back()

    steps = [
        {"run": {"name": "r", "request_id": "it-restart", "mode": "open_web"}},
        {"wait_state": {"name": "r", "state": "running"}},
        {"restart_proxy": {"wait": 1}},
        {"wait_state": {"name": "r", "state": "interrupted", "timeout": 60}},
        {"events": {"name": "r"}},
        {"notices": None},
        {"browser": None},
        {"proxy_status": None},
    ]
    out = run_lifecycle(steps, home=controller_home(tmp_path), on_ready=restart)
    assert out[3] == "interrupted"
    assert ["run_interrupted", "proxy_restarted"] in out[4]
    assert any(k == "run_interrupted" for k, _ in out[5])
    assert out[6] is False and out[7]["run_id"] is None    # never reinstalled
    assert not _chrome_processes()


def test_controller_kill_and_restart(tmp_path):
    def kill(proc, line):
        proc.send_signal(signal.SIGKILL)

    first = [
        {"run": {"name": "r", "request_id": "it-kill", "mode": "open_web"}},
        {"wait_state": {"name": "r", "state": "running"}},
        {"proxy_status": None},
        {"ready": {"hold": 600}},
    ]
    home = controller_home(tmp_path)
    assert run_lifecycle(first, home=home, on_ready=kill, killed=True) is None
    second = [
        {"states": None},
        {"notices": None},
        {"proxy_status": None},
        {"browser": None},
    ]
    out = run_lifecycle(second, home=home)
    assert set(out[0].values()) == {"interrupted"} and out[0]
    run_id = next(iter(out[0]))
    assert ["run_interrupted", run_id] in out[1]
    assert out[2]["run_id"] is None and out[3] is False
    assert not _chrome_processes()


def test_s5n_a_sign_in_card_registers_the_browsers_origin_and_signs_in(tmp_path):
    """Slice 5n, in real Chrome: an open-web run on a public sign-in page (an address with an
    uppercase host, the default port and a query); the card's host is the browser's, the Yes
    registers exactly that origin, and the run signs in on the new site's own profile with
    the one fitting item, by the broker's real login flow."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, A]) as sites:
        _controller_uid(sites)
        sites.pages["/username"] = form("username", "/password")
        sites.pages["/password"] = form("current-password", "/finish")
        sites.pages["/finish"] = ("text/html", b"", {"Location": f"https://{LOGIN}/done"})
        sites.status["/finish"] = 303
        sites.pages["/done"] = ("text/html", b"<title>Home</title><p>Signed in</p>", {})
        start = f"https://{LOGIN.upper()}:443/username?state=xyz"
        steps = [
            {"broker": {"op": "status"}},   # was the guest confirmation (gone, slice 7)
            {"run": {"name": "r", "request_id": "it-5n", "mode": "open_web",
                     "start_url": start}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sign_in": {"name": "r", "site_name": "Login Co", "verdict": {"items": [0],
                                                                         "reason": "fits"}}},
            {"question": {"name": "r"}},
            {"tap": {"name": "r", "choice": "confirm"}},
            {"sleep": 3.5},
            {"observe": {"name": "r"}},
            {"sites": True},
            {"events": {"name": "r"}},
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=controller_home(tmp_path), broker_fixture=config)
        assert out[3]["outcome"] == "asked", out
        view = out[4]["view"]
        assert view["kind"] == "site_signin" and view["host"] == LOGIN, view
        assert out[5]["outcome"] == "signing_in", out
        assert "Signed in" in out[7]["text"], out
        (site,) = out[8]
        assert site[0].startswith("site_") and site[1:] == [
            [f"https://{LOGIN}"], [f"https://{LOGIN}"], f"https://{LOGIN}/username", "item"], out[8]
        posts = [r for r in sites.receipts.requests if r[0] == "POST"]
        assert [p[2] for p in posts] == ["/password", "/finish"]
        assert out[10] == "completed"
