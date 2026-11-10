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


def test_a_public_excursion_keeps_a_session_cookie_sign_in(tmp_path):
    """Production controller, Chrome through the launcher, the enforcing proxy: a session cookie
    set in the account session is sent again after a public address and a checked address on the
    site, and the public site never gets it."""
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/start"] = ("text/html", b"<title>Account</title><p>Account home</p>",
                                 {"Set-Cookie": "sid=SessionOnly123; Secure; HttpOnly; Path=/"})
        sites.pages["/read"] = ("text/html", b"<title>Public</title><p>Public page</p>", {})
        sites.pages["/whoami"] = ("text/html", b"<title>Account</title><p>Who am I</p>", {})
        steps = [
            {"site": {"site_id": "sa", "name": "Site A", "origins": [f"https://{A}"],
                      "start": f"https://{A}/start"}},
            {"run": {"name": "r", "request_id": "it-away", "sites": [["sa", "account"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
            {"search": {"name": "r", "query": f"https://{B}/read"}},
            {"search": {"name": "r", "query": f"https://{A}/whoami"}},
            {"observe": {"name": "r"}},
            {"cancel": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=controller_home(tmp_path), broker_fixture=config)
        assert "Account home" in out[3]["text"]
        assert "Public page" in out[4]["text"]
        assert out[5]["outcome"] == "done" and "observation" not in out[5]
        assert "Who am I" in out[6]["text"]
        assert any(c[0].startswith(A) and c[1] == "/whoami" and "sid=SessionOnly123" in c[2]
                   for c in sites.receipts.cookies), sites.receipts.cookies
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


# A challenge page in front of a site, as measured on a real one: its widget's frames in a closed
# root go away with no navigation while it works, then it posts itself and its answer is the page.
# No field anywhere: the start page's sign-in only reads it, and sees frames go under the read.
_CHALLENGE = ("text/html", f"""<!doctype html><title>Just a moment</title><form id=done method=post
action="/signin"></form><div id=w></div><script>
const root = document.getElementById('w').attachShadow({{mode: 'closed'}});
const frames = [];
for (let i = 0; i < 40; i++) {{ const f = document.createElement('iframe');
  f.src = i % 2 ? 'https://{B}/widget' : '/widget'; root.append(f); frames.push(f); }}
setTimeout(() => {{ const go = setInterval(() => {{ const f = frames.pop();
  if (f) f.remove(); else clearInterval(go); }}, 75); }}, 1500);
setTimeout(() => document.getElementById('done').submit(), 6000);
</script>""".encode(), {})


@pytest.mark.parametrize("grants", [["s1"], ["s1", "s2"]], ids=["one_site", "two_sites"])
def test_a_challenge_page_whose_frames_go_away_does_not_end_the_run(tmp_path, grants):
    """A real site's challenge page ended two runs at their start (``broker_internal``): a frame
    went while the start page's sign-in was reading it. A page moving under a read ends nothing:
    the run starts and nothing is sent but the challenge's own post."""
    config = fixture_config(tmp_path)
    other = f"https://{LOGIN}"
    with ChromeSites([A, B, LOGIN]) as sites:
        _controller_uid(sites)
        sites.pages["/start"] = _CHALLENGE
        sites.pages["/signin"] = ("text/html", b"<title>Home</title><h1>Welcome</h1>", {})
        sites.pages["/widget"] = ("text/html", b"<div>widget</div>", {})
        out = run_lifecycle([
            {"site": {"site_id": "s1", "name": "Example", "origins": [f"https://{A}"],
                      "login": [f"https://{A}"], "item": "item", "start": f"https://{A}/start"}},
            {"site": {"site_id": "s2", "name": "Other", "origins": [other], "login": [other],
                      "item": "item", "start": f"{other}/start"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{A}"], "operator_ref": "op"}},
            {"broker": {"op": "register_item", "site_id": "s2", "item_id": "item",
                        "login_origins": [other], "operator_ref": "op"}},
            {"run": {"name": "r", "request_id": "it-challenge",
                     "sites": [[site, "account"] for site in grants]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"events": {"name": "r"}},
            {"finish": {"name": "r"}},
        ], home=controller_home(tmp_path), broker_fixture=config)
        kinds = out[6]
        assert ["run_started", "running"] in kinds, kinds
        assert "run_interrupted" not in [k for k, _o in kinds], kinds
        assert {r[2] for r in sites.receipts.requests if r[0] == "POST"} <= {"/signin"}
        assert out[7] == "completed", out


def _captcha_sign_in(render_ms, also_in_root=""):
    """A sign-in page as measured: a client-rendered form with a plain username, a button held
    until a CAPTCHA solves, the CAPTCHA's other-origin frame alone in a closed shadow root
    (Turnstile puts it there)."""
    return ("text/html", f"""<!doctype html><title>Sign In // Example</title><div id=app></div>
<script>setTimeout(() => {{
  document.getElementById('app').innerHTML = `<h1>Sign in to Example</h1>
    <form method=post action="/session"><label for=u>Username</label><input type=text id=u name=user>
    <label for=p>Password</label><input type=password id=p name=pass><div id=w></div>
    <button id=go type=submit disabled>Sign In</button></form>`;
  const root = document.getElementById('w').attachShadow({{mode: 'closed'}});
  const frame = document.createElement('iframe');
  frame.src = 'https://{B}/widget'; root.append(frame);
  {also_in_root}
  setTimeout(() => {{ document.getElementById('go').disabled = false; }}, 1500);
}}, {render_ms});</script>""".encode(), {})


def _captcha_run(tmp_path, page, *tail):
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/signin"] = page
        sites.pages["/widget"] = ("text/html", b"<div>widget</div>", {})
        sites.pages["/session"] = ("text/html", b"", {"Location": f"https://{A}/home"})
        sites.status["/session"] = 303
        sites.pages["/home"] = ("text/html", b"<title>Home</title><h1>Your decks</h1>", {})
        out = run_lifecycle([
            {"site": {"site_id": "s1", "name": "Example", "origins": [f"https://{A}"],
                      "login": [f"https://{A}"], "item": "item", "start": f"https://{A}/signin"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{A}"], "operator_ref": "op"}},
            {"run": {"name": "r", "request_id": "it-captcha", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 10},
            {"look": {"name": "r"}},
            *tail,
            {"events": {"name": "r"}},
            {"finish": {"name": "r"}},
        ], home=controller_home(tmp_path), broker_fixture=config)
        return out, [r for r in sites.receipts.requests if r[0] == "POST"]


@pytest.mark.parametrize("render_ms", [500, 9000])
def test_a_sign_in_form_beside_a_captcha_in_a_closed_shadow_root_is_signed_into(tmp_path, render_ms):
    """The start page's own form, whether its sign-in sent it to the check (500 ms) or gave up
    before it rendered (9 s, which leaves its event): the first observe looks and the verdict
    signs in with one POST."""
    sign_in = {"verdict": "sign_in", "reason": "the site's login form",
               "fields": {"username": "f1", "password": "f2"}}
    out, posts = _captcha_run(tmp_path, _captcha_sign_in(render_ms),
                              {"verdict": {"name": "r", "verdict": sign_in}})
    assert out[5]["outcome"] == "judgement_required", out
    assert out[6]["observation"]["notice"] == "Casa Browser filled in the sign-in of Example and sent it; read the page to see whether it worked.", out
    assert [p[2] for p in posts] == ["/session"], posts
    from urllib.parse import parse_qs
    body = parse_qs(posts[0][3].decode())
    assert body["user"][0] and body["pass"][0]
    kinds = [k for k, _o in out[7]]
    assert kinds.count("sign_in_sent") == 1 and "login_needed" not in kinds, out
    assert (["sign_in_nothing", "no_form"] in out[7]) == (render_ms == 9000), out
    assert out[8] == "completed"


# A password manager's button as measured in Chrome with 1Password: a closed root holding a
# button, a stylesheet link and a box, no field.
PASSWORD_MANAGER = """const pm = document.createElement('com-pm-button'); document.body.append(pm);
  pm.attachShadow({mode: 'closed'}).innerHTML = '<button>Fill</button><link rel=stylesheet href="data:text/css,"><div>menu</div>';"""


@pytest.mark.parametrize("announce", ["", """const line = document.createElement('div');
  line.setAttribute('aria-live', 'polite');
  line.textContent = 'The menu is available. Press down arrow to select.';
  document.body.append(line);"""])
def test_a_sign_in_form_beside_a_password_managers_button_is_signed_into(tmp_path, announce):
    """A closed root with no field keeps nothing from the check: one verdict, one POST, on the
    site's own origin, with the username and the password it mapped."""
    sign_in = {"verdict": "sign_in", "reason": "the site's login form",
               "fields": {"username": "f1", "password": "f2"}}
    out, posts = _captcha_run(tmp_path, _captcha_sign_in(500, PASSWORD_MANAGER + announce),
                              {"verdict": {"name": "r", "verdict": sign_in}})
    assert out[5]["outcome"] == "judgement_required", out
    assert out[6]["observation"]["notice"] == "Casa Browser filled in the sign-in of Example and sent it; read the page to see whether it worked.", out
    assert [p[2] for p in posts] == ["/session"] and posts[0][1] == A, posts
    assert ["sign_in_nothing", "not_checked"] not in out[7], out


def test_a_form_that_goes_away_on_a_refusal_claims_nothing(tmp_path):
    """The site answers the submit with a refusal and no form: no word of being signed in, and the
    model reads the refusal on the page."""
    refusing = """document.querySelector('form').addEventListener('submit', (e) => {
      e.preventDefault(); document.getElementById('app').innerHTML = '<h1>Account locked</h1>'; });
      setTimeout(() => { document.getElementById('go').disabled = false; }, 0);"""
    sign_in = {"verdict": {"name": "r", "verdict": {
        "verdict": "sign_in", "reason": "the site's login form",
        "fields": {"username": "f1", "password": "f2"}}}}
    out, posts = _captcha_run(tmp_path, _captcha_sign_in(500, refusing), sign_in)
    observation = out[6]["observation"]
    assert "read the page to see whether it worked" in observation["notice"], out
    assert "Account locked" in observation["text"], observation["text"]
    kinds = [k for k, _o in out[7]]
    assert kinds.count("sign_in_sent") == 1 and "signed_in" not in kinds, out[7]


SIGN_IN_VERDICT = {"verdict": "sign_in", "reason": "the site's login form",
                   "fields": {"username": "f1", "password": "f2"}}


def _background_sign_in(answer_ms):
    """A form that signs in by a background request, as a real site does: the form stays while its
    request runs and goes away only when (``answer_ms``) the answer comes, or never. A plain
    username, a redraw of the form's own part on input, a CAPTCHA's frame in a closed root and a
    password manager's button in another."""
    gone = ("" if answer_ms is None else
            "setTimeout(() => { document.getElementById('app').innerHTML = '<h1>Your decks</h1>'; }, %d);" % answer_ms)
    return _captcha_sign_in(500, PASSWORD_MANAGER + """
      const f = document.querySelector('form');
      f.addEventListener('input', () => { document.getElementById('w').dataset.n = Date.now(); });
      f.addEventListener('submit', (e) => { e.preventDefault();
        fetch('/session', {method: 'POST', body: new URLSearchParams(new FormData(f))}); %s });
      """ % gone)


@pytest.mark.parametrize("answer_ms", [1500, None], ids=["answered", "never_answered"])
def test_a_form_that_stays_while_its_request_runs_is_sent_once(tmp_path, answer_ms):
    """Measured on a real site: the form was still there when the flow read the page right after
    its send, and the sign-in worked 0.8 s later. The flow waits for the answer, pressing nothing;
    a form that stays goes to the live view, and either way exactly one POST left."""
    out, posts = _captcha_run(tmp_path, _background_sign_in(answer_ms),
                              {"verdict": {"name": "r", "verdict": SIGN_IN_VERDICT}})
    assert [p[2] for p in posts] == ["/session"], posts
    kinds = out[7]
    if answer_ms is None:
        assert ["login_needed", "password_again"] in kinds, kinds
    else:
        assert "read the page to see whether it worked" in out[6]["observation"]["notice"], out
        assert "Your decks" in out[6]["observation"]["text"], out[6]
        assert ["sign_in_sent", "submitted"] in kinds and "login_needed" not in [k for k, _o in kinds], kinds


_TICKING = """let tick = 0; const shown = document.querySelector('h1');
  setInterval(() => { shown.textContent = 'From ' + (++tick) + ' EUR'; }, 300);"""
_LINE_ON_FILL = """document.getElementById('u').addEventListener('input', () => {
  const line = document.createElement('div'); line.setAttribute('aria-live', 'polite');
  line.textContent = 'The menu is available.'; document.body.append(line); });"""
_ROTATING_SEARCH = """const search = document.createElement('form'); document.body.append(search);
  search.innerHTML = '<input type=search name=q placeholder=Search><button>Go</button>';
  let turn = 0; setInterval(() => { search.q.placeholder = 'Search ' + (++turn); }, 300);"""
_BANNER = """const banner = document.createElement('div'); banner.innerHTML = '<p>We use cookies.</p>'
  + '<form action="/consent" method=post><button>Accept all</button></form>'
  + '<form action="/consent" method=post><button>Only required</button></form>';
  document.body.prepend(banner);"""


@pytest.mark.parametrize("moving, between", [
    (_TICKING, None), (_LINE_ON_FILL, None), (_ROTATING_SEARCH, None), ("", _BANNER),
    (_LINE_ON_FILL, _BANNER)], ids=["price", "line_on_fill", "search_box", "banner", "banner_and_line"])
def test_what_moves_around_the_judged_form_never_stops_the_sign_in(tmp_path, moving, between):
    """Page text beside the form (a price, a password manager's line when a field fills, a cookie
    banner while the check runs) and a box the sign-in does not use: one judgement, one POST."""
    tail = [{"js": {"code": between}}] if between else []
    out, posts = _captcha_run(tmp_path, _captcha_sign_in(500, moving), *tail, {"sleep": 2.5},
                              {"verdict": {"name": "r", "verdict": SIGN_IN_VERDICT}})
    verdict, kinds = out[-3], out[-2]
    assert verdict["observation"]["notice"].endswith("read the page to see whether it worked."), out
    assert [p[2] for p in posts] == ["/session"], posts
    assert kinds.count(["judgement", "sign_in"]) == 1 and "sign_in_nothing" not in [k for k, _o in kinds], kinds


@pytest.mark.parametrize("once", [True, False], ids=["once", "every_time"])
def test_a_form_drawn_anew_after_the_look_is_judged_again_once(tmp_path, once):
    """New nodes are not the ones the look marked, so nothing goes out and the check judges the
    page as it now is. A form drawn anew once then signs in; one drawn anew again claims nothing."""
    redraw = {"js": {"code": "const f = document.querySelector('form'); f.outerHTML = f.outerHTML;"}}
    verdict = {"verdict": {"name": "r", "verdict": SIGN_IN_VERDICT}}
    out, posts = _captcha_run(tmp_path, _captcha_sign_in(500), redraw, verdict,
                              *([] if once else [redraw]), verdict)
    assert out[7]["outcome"] == "judgement_required", out
    kinds, final = out[-2], out[-3]
    assert kinds.count(["sign_in_nothing", "page_moved"]) == (1 if once else 2), kinds
    if once:
        assert final["observation"]["notice"].endswith("read the page to see whether it worked."), out
        assert [p[2] for p in posts] == ["/session"], posts
    else:
        assert final["observation"].get("notice") is None, out
        assert posts == [] and "sign_in_sent" not in [k for k, _o in kinds], (posts, kinds)


@pytest.mark.parametrize("field", [
    "const x = document.createElement('input'); x.type = 'password'; root.append(x);",
    """const d = document.createElement('div'); root.append(d);
       d.attachShadow({mode: 'closed'}).innerHTML = '<input type=password>';""",
    """const d = document.createElement('div'); root.append(d);
       d.attachShadow({mode: 'open'}).innerHTML = '<form><input type=password></form>';""",
    "root.innerHTML += '<div contenteditable=true autocomplete=one-time-code></div>';"],
    ids=["direct", "closed_in_closed", "open_in_closed", "editable"])
def test_a_field_in_a_closed_shadow_root_keeps_the_page_from_the_check(tmp_path, field):
    """A closed root holding a field, however deep, could hide the form's password: no judgement,
    nothing filled, and the run says so."""
    out, posts = _captcha_run(tmp_path, _captcha_sign_in(500, field))
    assert out[5].get("observation") is True, out
    assert posts == [], posts
    assert ["sign_in_nothing", "not_checked"] in out[6] and "sign_in_sent" not in [k for k, _o in out[6]], out
    assert out[7] == "completed"


def test_signing_in_off_the_start_page(tmp_path):
    """Signing in off the start page, in real Chrome with the production controller, driver and
    broker (the phone sequence of casa-test 2026-11-02): the site's start page is its root,
    which has no form (``no_form``, silent); Fido's judged step opens /login; observe with
    ``sign_in`` looks at it (timed) and returns the ``sign_in`` judgement; its verdict (scripted
    here; the real check's verdicts on this page shape are measured, M9) signs in on the page as
    it is: one POST, the secure area, one ``sign_in_sent``, no ``login_needed``. Then the reset
    page, judged ``not_sign_in``: nothing filled, nothing posted to it."""
    config = fixture_config(tmp_path)
    with ChromeSites([A]) as sites:
        _controller_uid(sites)
        sites.pages["/"] = ("text/html", b'<title>The Internet</title><h1>Welcome</h1>'
                            b'<a href="/login">Form Authentication</a>', {})
        # the-internet's form as served: its username marks no role (R9: the check maps it)
        sites.pages["/login"] = ("text/html", (
            b'<title>The Internet</title><h2>Login Page</h2><form method=post action="/authenticate">'
            b'<label for=username>Username</label><input type=text id=username name=username>'
            b'<label for=password>Password</label><input id=password name=password type=password>'
            b'<button type=submit>Login</button></form>'), {})
        sites.pages["/authenticate"] = ("text/html", b"", {"Location": f"https://{A}/secure"})
        sites.status["/authenticate"] = 303
        sites.pages["/secure"] = ("text/html", b'<title>The Internet</title><h2>Secure Area</h2>'
                                  b'<p>You logged into a secure area!</p><a href="/logout">Logout</a>'
                                  b' <a href="/forgot_password">Forgot password</a>', {})
        sites.pages["/forgot_password"] = ("text/html", (
            b'<title>The Internet</title><h2>Forgot Password</h2><form method=post action="/forgot_password">'
            b'<label for=email>E-mail</label><input type=email id=email name=email>'
            b'<button type=submit>Retrieve password</button></form>'), {})
        sign_in = {"verdict": "sign_in", "reason": "the site's login form",
                   "fields": {"username": "f1", "password": "f2"}}
        not_sign_in = {"verdict": "not_sign_in", "reason": "a password reset form"}
        steps = [
            {"site": {"site_id": "s1", "name": "The Internet", "origins": [f"https://{A}"],
                      "login": [f"https://{A}"], "item": "item", "start": f"https://{A}/"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{A}"], "operator_ref": "op"}},
            {"run": {"name": "r", "request_id": "it-look", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 3.5},     # the credential window's quiet interval after close_window
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "navigate", "label": "Form Authentication"}},
            {"look": {"name": "r"}},
            {"verdict": {"name": "r", "verdict": sign_in}},
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "navigate", "label": "Forgot password"}},
            {"look": {"name": "r"}},
            {"verdict": {"name": "r", "verdict": not_sign_in}},
            {"events": {"name": "r"}},
            {"finish": {"name": "r"}},
        ]
        out = run_lifecycle(steps, home=controller_home(tmp_path), broker_fixture=config)
        assert out[3] == "running", out
        look = out[7]
        assert look["outcome"] == "judgement_required", out
        assert look["judgement"]["kind"] == "sign_in"
        done = out[8]
        assert done["outcome"] == "done", done
        assert done["observation"]["notice"] == "Casa Browser filled in the sign-in of The Internet and sent it; read the page to see whether it worked."
        assert "Secure Area" in done["observation"]["text"], done
        assert out[11]["outcome"] == "judgement_required", out
        assert out[12]["outcome"] == "done" and out[12]["observation"]["notice"] is None, out
        posts = [r for r in sites.receipts.requests if r[0] == "POST"]
        assert [p[2] for p in posts] == ["/authenticate"], posts
        from urllib.parse import parse_qs
        body = parse_qs(posts[0][3].decode())
        assert body["username"][0] and body["password"][0]   # the plain username was filled
        kinds = [k for k, _o in out[13]]
        assert kinds.count("sign_in_sent") == 1 and "login_needed" not in kinds, out[13]
        print("LOOK_SECONDS", look["seconds"], out[11]["seconds"])
        assert out[14] == "completed"
