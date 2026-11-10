"""Task 8, in the built image: branded Chrome as the browser user, driven from the
controller user, under the production launcher, floor and managed policy."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import proxyharness as h
from browserprobe import run_scenario
from probes import as_user
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker

SITE_A, SITE_B = "site-a.example", "site-b.example"


def _chrome_processes() -> list[tuple[int, int]]:
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            # cmdline, not exe: Chrome marks itself non-dumpable, which makes /proc/<pid>/exe
            # unreadable even to root in a container without SYS_PTRACE.
            try:
                cmd = (p / "cmdline").read_bytes()
                uid = int((p / "status").read_text().split("Uid:")[1].split()[0])
            except (OSError, IndexError, ValueError):
                continue
            if cmd.startswith(b"/opt/google/chrome/"):
                out.append((int(p.name), uid))
    return out


def test_launch_arguments():
    """The namespace sandbox is active, the command line carries neither --no-sandbox nor
    --disable-background-networking, and Chrome runs as the browser user."""
    res = run_scenario([{"open": {"site_id": None, "mode": "open_web"}},
                        {"goto": "chrome://sandbox"}, {"text": True},
                        {"goto": "chrome://version"}, {"eval": "() => document.querySelector('#command_line').innerText"},
                        {"eval": "() => 1"}])
    sandbox, cmdline = res[2], res[4]
    assert "Namespace" in sandbox and "adequately sandboxed" in sandbox, sandbox[:400]
    assert "--no-sandbox" not in cmdline
    assert "--disable-background-networking" not in cmdline
    assert "--remote-debugging-port" not in cmdline
    assert not _chrome_processes()  # closed: nothing left behind


def test_chrome_runs_as_the_browser_user():
    code = r"""
import asyncio, os, sys
sys.path.insert(0, "/opt")
from pathlib import Path
from casa_browser.browser import BrowserManager
from casa_browser.protocol import EffectiveLocale
async def main():
    m = BrowserManager(placeholder_dir=Path("/tmp/casa-it-controller/placeholder"), gpu=False)
    await m.open(None, "open_web", EffectiveLocale("en-GB", "UTC", "option"))
    uids = set()
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if (p / "cmdline").read_bytes().startswith(b"/opt/google/chrome/"):
                    uids.add(int((p / "status").read_text().split("Uid:")[1].split()[0]))
            except OSError:
                pass
    print("UIDS", sorted(uids))
    await m.shutdown()
asyncio.run(main())
"""
    os.makedirs("/tmp/casa-it-controller", exist_ok=True)
    os.chown("/tmp/casa-it-controller", 1103, 1103)
    r = subprocess.run(["setpriv", "--reuid=casa-controller", "--regid=casa-controller", "--clear-groups",
                        "/opt/venv/bin/python", "-c", code], capture_output=True, text=True, timeout=180,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/tmp/casa-it-controller"})
    line = next(l for l in r.stdout.splitlines() if l.startswith("UIDS"))
    assert line == "UIDS [1100]", (line, r.stderr[-1500:])


def test_chrome_signin_disabled():
    """Each managed policy is in effect with the intended value (Machine, Mandatory, OK)."""
    rows = set(run_scenario([{"open": {"site_id": None, "mode": "open_web"}},
                             {"policy_rows": True}])[1])
    for name, value in [("BrowserSignin", "0"), ("PasswordManagerEnabled", "false"),
                        ("PasswordLeakDetectionEnabled", "false"), ("SyncDisabled", "true"),
                        ("MetricsReportingEnabled", "false"), ("DnsOverHttpsMode", "off"),
                        ("BuiltInDnsClientEnabled", "false"), ("QuicAllowed", "false"),
                        ("WebRtcIPHandling", "disable_non_proxied_udp"),
                        ("CommandLineFlagSecurityWarningsEnabled", "false"),
                        ("DeveloperToolsAvailability", "1")]:
        assert f"{name} {value} Platform Machine Mandatory OK" in rows, (name, sorted(rows))
    ext = [r for r in rows if r.startswith("ExtensionSettings ")]
    assert ext and ext[0].endswith("Platform Machine Mandatory OK")
    assert '"installation_mode":"force_installed"' in ext[0]
    assert '"toolbar_pin":"force_pinned"' in ext[0]      # Chrome validates the key: still OK


def _chrome_ui_height() -> int:
    """Chrome's own UI above the page (tab strip, toolbar and any infobar), in px."""
    res = run_scenario([{"open": {"site_id": None, "mode": "open_web"}}, {"wait": 3},
                        {"eval": "() => outerHeight - innerHeight"}])
    return res[2]


def test_no_command_line_flag_warning_bar():
    """casa-test F28: Chrome showed "You are using an unsupported command-line flag:
    --disable-blink-features…" over every page (Patchright's flag, which keeps
    navigator.webdriver false). The managed policy hides it: the room above the page is the
    tab strip and the toolbar only. Positive control: the same launch with the policy's
    warnings turned back on shows the bar, which takes its own room."""
    policy = Path("/etc/opt/chrome/policies/managed/casa.json")
    shipped = policy.read_text()
    assert json.loads(shipped)["CommandLineFlagSecurityWarningsEnabled"] is False
    hidden = _chrome_ui_height()
    try:
        policy.write_text(json.dumps(dict(json.loads(shipped), CommandLineFlagSecurityWarningsEnabled=True)))
        shown = _chrome_ui_height()
    finally:
        policy.write_text(shipped)
    print("chrome ui height: hidden", hidden, "shown", shown)
    assert shown - hidden >= 25, (hidden, shown)     # the bar is there only with the warnings on
    assert _chrome_ui_height() == hidden              # and gone again with the shipped policy


def test_floor_failure_prevents_browser_start():
    """The launcher half of Task 5's test: without the floor marker no Chrome starts."""
    marker = Path("/run/casa-browser/netfloor.ok")
    saved = marker.read_bytes()
    marker.unlink()
    try:
        with pytest.raises(RuntimeError) as err:
            run_scenario([{"open": {"site_id": None, "mode": "open_web"}}], timeout=120)
        assert "no_floor" in str(err.value)
        assert not _chrome_processes()
    finally:
        marker.write_bytes(saved)
        os.chmod(marker, 0o644)


def _account_policy(site_ids, origins_for, active=None, revision=1):
    sites = tuple(h.site(sid, origins=(origins_for[sid],), login=()) for sid in site_ids)
    return h.account_policy(sites=sites if active is None else
                            tuple(sorted(sites, key=lambda s: s.site_id != active)), revision=revision)


def test_account_profile_survives_restart_and_profiles_stay_apart():
    with ChromeSites([SITE_A, SITE_B]) as sites:
        sites.pages["/login"] = ("text/html", b"<html><body>in</body></html>",
                                 {"Set-Cookie": "sid=a-secret; Secure; Path=/; Max-Age=86400"})
        origins = {"sa": f"https://{SITE_A}", "sb": f"https://{SITE_B}"}
        sites.install(_account_policy(["sa"], origins))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": f"https://{SITE_A}/login"},
                            {"cookies": f"https://{SITE_A}/"}, {"close": True},
                            {"open": {"site_id": "sa"}}, {"goto": f"https://{SITE_A}/check"},
                            {"cookies": f"https://{SITE_A}/"}])
        assert res[1] == 200 and res[2] == ["sid"] and res[6] == ["sid"]
        assert any(c[0] == SITE_A and c[1] == "/check" and "sid=a-secret" in c[2]
                   for c in sites.receipts.cookies)
        # Site B's profile and the public session carry nothing of site A.
        sites.receipts.reset()
        sites.install(_account_policy(["sb"], origins, revision=2))
        res = run_scenario([{"open": {"site_id": "sb"}}, {"cookies": f"https://{SITE_A}/"},
                            {"goto": f"https://{SITE_B}/check"},
                            {"open": {"site_id": None, "mode": "open_web"}},
                            {"cookies": f"https://{SITE_A}/"}])
        assert res[1] == [] and res[4] == []
        assert not sites.receipts.cookies
        # Forget site A: its profile goes, site B's stays.
        assert Path("/data/profiles/sa").exists() and Path("/data/profiles/sb").exists()
        run_scenario([{"forget": "sa"}])
        assert not Path("/data/profiles/sa").exists() and Path("/data/profiles/sb").exists()


def test_profiles_are_private_to_the_browser_user():
    run_scenario([{"open": {"site_id": "sp"}}, {"close": True}])
    profiles = list(Path("/data/profiles").iterdir())
    assert any(d.name == "sp" for d in profiles)
    for d in profiles:
        st = d.stat()
        assert st.st_uid == 1100 and (st.st_mode & 0o077) == 0, d
    r = as_user("controller", "import os; print(os.listdir('/data/profiles'))")
    assert r.returncode != 0  # PermissionError: the controller cannot list profiles


def test_visible_click_timeout():
    with ChromeSites([SITE_A]) as sites:
        sites.pages["/buttons"] = ("text/html", b"<html><body><button>Pay</button>"
                                   b"<button style='display:none'>Pay</button></body></html>", {})
        sites.install(h.open_web_policy())
        res = run_scenario([{"open": {"site_id": None, "mode": "open_web"}},
                            {"goto": f"https://{SITE_A}/buttons"},
                            {"click_hidden_duplicate": "Pay"}])
        assert res[2]["code"] == "not_visible"
        assert res[2]["seconds"] <= 4.0            # told after a moment, not the scroll's timeout



def test_policy_extension_installed_after_new_profile():
    """Task 8 Step 4: the managed policy installs the official 1Password extension, enabled and
    not removable, into two new profiles and after a restart — through the production proxy,
    which admits only the exact Chrome update hosts (real Web Store; needs the internet)."""
    from probes import install_policy, revoke
    for d in ("ext-a", "ext-b"):
        subprocess.run(["rm", "-rf", f"/data/profiles/{d}"], check=True)
    s_a = h.site("ext-a", origins=("https://example.com",), login=())
    s_b = h.site("ext-b", origins=("https://example.com",), login=())
    try:
        install_policy(h.account_policy(sites=(s_a, s_b), revision=1))
        res = run_scenario([{"open": {"site_id": "ext-a"}}, {"extension": 150}], timeout=400)
        assert res[1] and res[1]["state"] == "ENABLED", res
        assert res[1]["mustRemainInstalled"] is True and res[1]["userMayModify"] is False
        install_policy(h.account_policy(sites=(s_b, s_a), revision=2))
        res = run_scenario([{"open": {"site_id": "ext-b"}}, {"extension": 150}], timeout=400)
        assert res[1] and res[1]["state"] == "ENABLED", res
        install_policy(h.account_policy(sites=(s_a, s_b), revision=3))
        res = run_scenario([{"open": {"site_id": "ext-a"}}, {"extension": 10}], timeout=200)
        assert res[1] and res[1]["state"] == "ENABLED", res  # still there after a restart
    finally:
        revoke()


def test_the_extensions_own_background_calls_are_never_a_question():
    """casa-test F10: the 1Password extension calls 1Password hosts on its own at every start
    (measured: ``POST my.1password.com/api/pre-registration-features`` with ``Origin:
    chrome-extension://…``, ``GET watchtower.1password.com/api/v1/…`` without one; both
    ``Sec-Fetch-Site: none``). Outside the account's own admitted hosts they are refused as
    ``browser_background`` — never a candidate the operator is asked about. Positive control:
    the extension did call (real Web Store and 1Password; needs the internet)."""
    from urllib.parse import urlsplit
    from probes import install_policy, revoke
    subprocess.run(["rm", "-rf", "/data/profiles/ext-f"], check=True)
    s_f = h.site("ext-f", origins=("https://example.com",), login=())
    try:
        install_policy(h.account_policy(sites=(s_f,), revision=1))
        # The proxy starts with its credential window open (events then keep no origin); the
        # controller closes it once it is up — closed here the same way. A close keeps origins
        # out for a quiet period after it (WINDOW_QUIET), and the extension's first calls come
        # about 2.6 s after Chrome starts (measured): the browser starts only once the proxy
        # reports the window closed, or the control's calls can arrive without their origin.
        r = as_user("controller", "import asyncio\nfrom casa_browser.proxy_client import ProxyClient\n"
                    "async def main():\n    c = ProxyClient()\n    await c.close_window(await c.open_window())\n"
                    "    for _ in range(300):\n        if not (await c.status())['window_open']:\n            return\n"
                    "        await asyncio.sleep(0.1)\n    raise SystemExit('the window stayed open')\n"
                    "asyncio.run(main())")
        assert r.returncode == 0, r.stderr[-400:]
        res = run_scenario([{"open": {"site_id": "ext-f"}}, {"extension": 150}, {"wait": 150}],
                           timeout=500)
        assert res[1] and res[1]["state"] == "ENABLED", res
        r = as_user("controller", "import asyncio, json\nfrom casa_browser.proxy_client import ProxyClient\n"
                    "ev, _ = asyncio.run(ProxyClient().events(0))\n"
                    "print('EVENTS', json.dumps([[e.origin, e.allow, e.reason] for e in ev]))")
        line = next(l for l in r.stdout.splitlines() if l.startswith("EVENTS "))
        events = json.loads(line[len("EVENTS "):])
    finally:
        revoke()
    onepassword = [e for e in events if e[0] and "1password" in (urlsplit(e[0]).hostname or "")]
    assert onepassword, ("the extension called no 1Password host: the control did not run",
                         sorted({(e[0], e[2]) for e in events}, key=str))
    assert all(not allow and reason == "browser_background" for _, allow, reason in onepassword), onepassword


def test_gpu_flags_launch_without_a_gpu():
    """The production ANGLE-Vulkan flags must not stop Chrome where no GPU is present (CI);
    hardware acceleration itself is measured on the target box, not here."""
    res = run_scenario([{"open": {"site_id": None, "mode": "open_web"}}, {"goto": "chrome://gpu"},
                        {"wait": 2}, {"text": True}], gpu=True)
    assert "Graphics Feature Status" in res[3]


def test_a_restarted_launcher_leaves_no_chrome_behind():
    """The launcher dies while a Chrome runs: s6 restarts it, and the new launcher kills the
    leftover before serving, so no Chrome runs unsupervised and none can overlap a new one."""
    import json as _json
    import signal
    import threading
    import time
    from browserprobe import run_scenario as rs

    t = threading.Thread(target=lambda: _swallow(rs, [{"open": {"site_id": None, "mode": "open_web"}},
                                                      {"wait": 40}]), daemon=True)
    t.start()
    for _ in range(100):
        if _chrome_processes():
            break
        time.sleep(0.3)
    assert _chrome_processes(), "no Chrome started"
    launcher_pid = next(int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()
                        and b"casa_browser.launcher" in (p / "cmdline").read_bytes())
    os.kill(launcher_pid, signal.SIGKILL)
    for _ in range(100):
        if not _chrome_processes():
            break
        time.sleep(0.3)
    assert not _chrome_processes(), _chrome_processes()
    t.join(60)


def _swallow(fn, steps):
    try:
        fn(steps, timeout=120)
    except Exception:
        pass
