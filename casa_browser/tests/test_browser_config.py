"""Task 8 (pure part): the Chrome launch contract, the managed policy, the browser-user
launcher's checks and the pacing primitives."""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import socket
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from casa_browser import browser, launcher, login, pacing
from casa_browser.broker import BrokerError
from casa_browser.launcher import LaunchRefused
from casa_browser.protocol import EffectiveLocale

from conftest import APP_DIR

POLICY = json.loads((APP_DIR / "rootfs/etc/opt/chrome/policies/managed/casa.json").read_text())
ONEPASSWORD_EXTENSION = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"
LOCALE = EffectiveLocale("de-DE", "Europe/Amsterdam", "home_assistant")


# --------------------------------------------------------------------------- launch contract


def test_launch_arguments():
    kw = browser.launch_options(profile="site:s1", locale=LOCALE)
    assert kw["chromium_sandbox"] is True          # Patchright's default adds --no-sandbox
    assert kw["ignore_default_args"] == ["--disable-background-networking"]
    assert kw["headless"] is False and kw["no_viewport"] is True
    assert kw["service_workers"] == "allow"
    assert kw["accept_downloads"] is True and kw["downloads_path"] == "/data/downloads"
    assert kw["executable_path"] == str(browser.CHROME_WRAPPER)
    args = kw["args"]
    assert "--proxy-server=http://127.0.0.1:3128" in args
    assert "--proxy-bypass-list=<-loopback>" in args  # no implicit loopback bypass
    assert "--lang=de-DE" in args
    assert "--window-size=1920,1080" in args
    for flag in ("--use-angle=vulkan",
                 "--enable-features=CDPScreenshotNewSurface,Vulkan,DefaultANGLEVulkan,VulkanFromANGLE"):
        assert flag in args
    assert not any(a.startswith(("--no-sandbox", "--remote-debugging-port", "--user-agent",
                                 "--headless", "--load-extension")) for a in args)
    assert kw["env"] == {"CASA_BROWSER_PROFILE": "site:s1", "TZ": "Europe/Amsterdam"}
    assert "user_agent" not in kw and "extra_http_headers" not in kw
    assert browser.launch_options(profile="ephemeral", locale=LOCALE, gpu=False)["args"].count(
        "--use-angle=vulkan") == 0


def test_no_restore_bubble_and_the_launcher_admits_its_switch():
    """A tab Chrome restores under Patchright never finishes loading: the bubble that offers
    it must not be shown, and the launcher's allowlist must let the switch through."""
    args = browser.launch_options(profile="site:s1", locale=LOCALE)["args"]
    assert args.count("--hide-crash-restore-bubble") == 1
    assert "--hide-crash-restore-bubble" in launcher.ALLOWED_FLAGS


def test_profile_for_session():
    assert browser.profile_for("account", "s1") == "site:s1"
    assert browser.profile_for("public", None) == "ephemeral"
    for kind, site in [("account", None), ("public", "s1"), ("other", None), ("account", "../x")]:
        with pytest.raises(ValueError):
            browser.profile_for(kind, site)


def test_chrome_signin_disabled():
    """The managed policy file (effective-policy inspection is the image test)."""
    assert POLICY["BrowserSignin"] == 0
    assert POLICY["PasswordManagerEnabled"] is False
    assert POLICY["PasswordLeakDetectionEnabled"] is False   # S0 e: typing a password POSTs to Google
    assert POLICY["SyncDisabled"] is True
    assert POLICY["MetricsReportingEnabled"] is False
    assert POLICY["DnsOverHttpsMode"] == "off" and POLICY["BuiltInDnsClientEnabled"] is False
    assert POLICY["QuicAllowed"] is False
    assert POLICY["WebRtcIPHandling"] == "disable_non_proxied_udp"
    # casa-test F28: Patchright's --disable-blink-features=AutomationControlled (it keeps
    # navigator.webdriver false) is on Chrome's "unsupported flag" list; its warning bar is hidden
    assert POLICY["CommandLineFlagSecurityWarningsEnabled"] is False
    # Slice 5w (measured on Chrome 156): Chrome refuses the debugger on a force-installed
    # extension's pages by default, so the driver could not read the 1Password popup it opened.
    assert POLICY["DeveloperToolsAvailability"] == 1
    # Live view option C (casa-test F35): no form-history or autofill dropdown over the fields;
    # AutofillAddressEnabled also turns off single-field history (the image lane pins it).
    assert POLICY["AutofillAddressEnabled"] is False and POLICY["AutofillCreditCardEnabled"] is False
    ext = POLICY["ExtensionSettings"]
    assert set(ext) == {ONEPASSWORD_EXTENSION}
    # Pinned to the toolbar, so the operator finds 1Password in a live view (phone check
    # 2026-11-02).
    assert ext[ONEPASSWORD_EXTENSION] == {
        "installation_mode": "force_installed",
        "update_url": "https://clients2.google.com/service/update2/crx",
        "toolbar_pin": "force_pinned"}


# --------------------------------------------------------------------------- launcher checks

ARGS = ["/opt/casa_browser/bin/chrome-launch", "--disable-field-trial-config", "--no-first-run",
        "--user-data-dir=/run/casa-browser/controller/placeholder", "--remote-debugging-pipe",
        "--proxy-server=http://127.0.0.1:3128", "--proxy-bypass-list=<-loopback>", "about:blank"]


def test_argv_checked_and_profile_substituted(tmp_path):
    home = tmp_path / "home"
    paths = launcher.LauncherPaths(profiles=tmp_path / "profiles", home=home)
    argv, cleanup = launcher.chrome_argv(ARGS, "site:s1", paths)
    assert argv[0] == str(launcher.CHROME)
    assert f"--user-data-dir={tmp_path / 'profiles' / 's1'}" in argv
    assert not any("placeholder" in a for a in argv)
    assert stat.S_IMODE((tmp_path / "profiles" / "s1").stat().st_mode) == 0o700
    assert cleanup is None
    argv, cleanup = launcher.chrome_argv(ARGS, "ephemeral", paths)
    eph = Path(next(a for a in argv if a.startswith("--user-data-dir=")).split("=", 1)[1])
    assert eph.is_dir() and eph.parent == home / "ephemeral"
    cleanup()
    assert not eph.exists()


# The argv Patchright 1.64.1 passes for ``launch_options`` (captured from a real launch with a
# dumping executable). If an upgrade changes it, the launcher refuses and this test says why.
PATCHRIGHT_ARGV = ["/opt/casa_browser/bin/chrome-launch", "--disable-field-trial-config",
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-breakpad", "--no-default-browser-check", "--disable-dev-shm-usage",
    "--disable-edgeupdater",
    "--disable-features=AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,"
    "DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,PaintHolding,"
    "ThirdPartyStoragePartitioning,BlockOriginHeaderModificationOnRedirect,"
    "AvoidCorsURLLoaderRestartOnRedirect,Translate,AutoDeElevate,OptimizationHints,"
    "NetworkTimeServiceQuerying,AimEnabled,msForceBrowserSignIn,msEdgeUpdateLaunchServicesPreferredVersion",
    "--enable-features=CDPScreenshotNewSurface", "--disable-hang-monitor", "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding", "--disable-updater-scheduler", "--force-color-profile=srgb",
    "--no-first-run", "--password-store=basic", "--use-mock-keychain", "--no-service-autorun",
    "--export-tagged-pdf", "--disable-search-engine-choice-screen", "--edge-skip-compat-layer-relaunch",
    "--disable-infobars", "--disable-search-engine-choice-screen", "--disable-sync",
    "--disable-blink-features=AutomationControlled", "--proxy-server=http://127.0.0.1:3128",
    "--proxy-bypass-list=<-loopback>", "--lang=en-GB", "--window-position=0,0",
    "--window-size=1920,1080", "--hide-crash-restore-bubble", "--use-angle=vulkan",
    "--enable-features=CDPScreenshotNewSurface,Vulkan,DefaultANGLEVulkan,VulkanFromANGLE",
    "--user-data-dir=/run/casa-browser/controller/placeholder", "--remote-debugging-pipe", "about:blank"]


def test_patchrights_own_argv_is_admitted(tmp_path):
    paths = launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h")
    argv, _ = launcher.chrome_argv(PATCHRIGHT_ARGV, "site:s1", paths)
    assert len(argv) == len(PATCHRIGHT_ARGV)


@pytest.mark.parametrize("bad", [
    ["--no-sandbox"], ["--remote-debugging-port=9222"], ["--remote-debugging-address=0.0.0.0"],
    ["--headless"], ["--load-extension=/tmp/x"], ["--disable-extensions-except=/tmp/x"],
    ["https://example.com/"], ["--user-data-dir=/data/profiles/other", "--user-data-dir=/x"],
    ["--disable-namespace-sandbox"], ["--disable-seccomp-filter-sandbox"], ["--disable-setuid-sandbox"],
    ["--single-process"], ["--no-zygote"], ["--disable-gpu-sandbox"], ["--disable-web-security"],
    ["--proxy-server=http://203.0.113.9:8080"], ["--proxy-bypass-list=*"], ["--proxy-server=direct://"],
    ["--enable-features=NetworkServiceInProcess"], ["--disable-features=NetworkServiceSandbox"],
    ["--enable-features="], ["--lang=../../x"], ["--remote-debugging-pipe=cbor"],
    ["--renderer-cmd-prefix=/bin/sh"], ["--utility-cmd-prefix=gdb"], ["--remote-allow-origins=*"],
])
def test_argv_refusals(tmp_path, bad):
    paths = launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h")
    with pytest.raises(LaunchRefused):
        launcher.chrome_argv(ARGS + bad, "site:s1", paths)


@pytest.mark.parametrize("required", launcher.REQUIRED_ARGS)
def test_argv_requires_the_pipe_and_the_proxy_once(tmp_path, required):
    paths = launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h")
    with pytest.raises(LaunchRefused):
        launcher.chrome_argv([a for a in ARGS if a != required], "site:s1", paths)
    with pytest.raises(LaunchRefused):
        launcher.chrome_argv(ARGS[:-1] + [required, "about:blank"], "site:s1", paths)


@pytest.mark.parametrize("profile", ["site:", "site:../x", "site:a/b", "site:" + "x" * 65, "s1",
                                     "ephemeral:x", "", None])
def test_profile_refusals(tmp_path, profile):
    paths = launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h")
    with pytest.raises(LaunchRefused):
        launcher.chrome_argv(ARGS, profile, paths)


def test_symlinked_profile_refused(tmp_path):
    paths = launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h")
    (tmp_path / "p").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "p" / "s1").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(LaunchRefused):
        launcher.chrome_argv(ARGS, "site:s1", paths)


def test_environment_is_an_allowlist():
    env = launcher.chrome_env({"TZ": "Europe/Amsterdam", "LD_PRELOAD": "/tmp/x.so",
                               "CASA_BROWSER_PROFILE": "site:s1", "HTTPS_PROXY": "x"},
                              home=Path("/run/casa-browser/browser-home"))
    assert env["TZ"] == "Europe/Amsterdam" and env["DISPLAY"] == ":99"
    assert env["HOME"] == "/run/casa-browser/browser-home"
    assert "LD_PRELOAD" not in env and "HTTPS_PROXY" not in env and "CASA_BROWSER_PROFILE" not in env
    assert launcher.chrome_env({"TZ": "../../etc/passwd"}, home=Path("/h"))["TZ"] == "UTC"


def test_launcher_refuses_without_the_floor(tmp_path):
    server = launcher.Launcher(proc=_no_processes(), paths=launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h"),
                               controller_uid=os.getuid(), floor=lambda: False)
    with pytest.raises(LaunchRefused) as err:
        server.check_floor()
    assert err.value.code == "no_floor"


def test_forget_removes_only_the_selected_profile(tmp_path):
    paths = launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h")
    for site in ("a", "b"):
        (tmp_path / "p" / site / "Default").mkdir(parents=True)
        (tmp_path / "p" / site / "Default" / "Cookies").write_text(site)
    server = launcher.Launcher(proc=_no_processes(), paths=paths, controller_uid=os.getuid(), floor=lambda: True)
    server.forget("a")
    assert not (tmp_path / "p" / "a").exists()
    assert (tmp_path / "p" / "b" / "Default" / "Cookies").read_text() == "b"
    with pytest.raises(LaunchRefused):
        server.forget("../b")


def test_one_chrome_at_a_time(tmp_path):
    server = launcher.Launcher(proc=_no_processes(), paths=launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h"),
                               controller_uid=os.getuid(), floor=lambda: True)
    assert server.acquire() is True
    assert server.acquire() is False
    server.release()
    assert server.acquire() is True


def test_launch_socket_admits_only_the_controller(tmp_path):
    path = tmp_path / "launch.sock"
    server = launcher.Launcher(proc=_no_processes(), paths=launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h"),
                               controller_uid=os.getuid() + 1, floor=lambda: True)
    srv = server.serve(path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(path))
        s.sendall(json.dumps({"op": "forget", "site_id": "a"}).encode() + b"\n")
        s.settimeout(5)
        assert json.loads(s.recv(4096)) == {"ok": False, "error": "not_controller"}
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- pacing


class FakeMouse:
    def __init__(self, log):
        self.log = log

    async def move(self, x, y, steps=1):
        self.log.append(("move", round(x), round(y)))

    async def down(self):
        self.log.append(("down",))

    async def up(self):
        self.log.append(("up",))

    async def wheel(self, dx, dy):
        self.log.append(("wheel", dx, dy))


class FakeKeyboard:
    def __init__(self, log):
        self.log = log

    async def type(self, ch):
        self.log.append(("key", ch))


class FakePage:
    def __init__(self):
        self.log = []
        self.mouse = FakeMouse(self.log)
        self.keyboard = FakeKeyboard(self.log)
        self.viewport_size = None

    async def evaluate(self, expr, arg=None):
        if "scrollTop" in expr:
            return [[0, 0]]
        if "elementFromPoint" in expr:
            return False          # no form control under any point
        if "innerWidth" in expr:
            return [1920, 1080]
        return {"width": 1920, "height": 1080}


class FakeLocator:
    def __init__(self, page, visible=True, enabled=True, box=(400, 300, 100, 40), hang=False):
        self.page, self.visible, self.enabled, self.box, self.hang = page, visible, enabled, box, hang

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def scroll_into_view_if_needed(self, timeout=None):
        self.page.log.append(("scroll_into_view",))
        if self.hang:
            await asyncio.sleep(3600)

    async def bounding_box(self):
        x, y, w, h = self.box
        return {"x": x, "y": y, "width": w, "height": h}

    async def focus(self):
        self.page.log.append(("focus",))

    async def evaluate(self, expr, arg=None):       # the pacer selects what a field holds
        self.page.log.append(("select",))
        return True


def _pacer(page, **kw):
    delays = []

    async def sleep(d):
        delays.append(d)

    p = pacing.Pacer(page, rng=random.Random(7), sleep=sleep, **kw)
    return p, delays


def test_click_scrolls_into_view_then_moves_along_a_curve():
    page = FakePage()
    p, delays = _pacer(page)
    asyncio.run(p.click(FakeLocator(page)))
    kinds = [e[0] for e in page.log]
    assert kinds[0] == "scroll_into_view"
    moves = [e for e in page.log if e[0] == "move"]
    assert len(moves) >= 8                                   # a path, not a jump
    x, y = moves[-1][1], moves[-1][2]
    assert 400 <= x <= 500 and 300 <= y <= 340               # inside the target
    straight = all(abs((m[2] - moves[0][2]) * (x - moves[0][1]) - (m[1] - moves[0][1]) * (y - moves[0][2])) < 1
                   for m in moves)
    assert not straight                                      # curved
    assert kinds[-2:] == ["down", "up"]
    assert all(0 < d <= 0.5 for d in delays)


def test_type_has_bounded_per_character_delay():
    page = FakePage()
    p, delays = _pacer(page)
    asyncio.run(p.type(FakeLocator(page), "hello"))
    assert [e[1] for e in page.log if e[0] == "key"] == list("hello")
    key_delays = delays[-5:]
    assert all(pacing.KEY_DELAY[0] <= d <= pacing.KEY_DELAY[1] for d in key_delays)
    assert len(set(key_delays)) > 1


def test_scroll_in_steps():
    page = FakePage()
    p, _ = _pacer(page)
    asyncio.run(p.scroll("down", "page"))
    wheels = [e for e in page.log if e[0] == "wheel"]
    assert len(wheels) >= 3 and all(w[2] > 0 for w in wheels)
    page.log.clear()
    asyncio.run(p.scroll("up", "step"))
    assert all(e[2] < 0 for e in page.log if e[0] == "wheel")


@pytest.mark.parametrize("loc, code", [(dict(visible=False), "not_visible"),
                                       (dict(enabled=False), "disabled"),
                                       (dict(box=None), "no_size")])
def test_a_refused_target_says_why_at_once(loc, code):
    page = FakePage()
    p, _ = _pacer(page)
    locator = FakeLocator(page, **{k: v for k, v in loc.items() if k != "box"})
    if "box" in loc:
        locator.bounding_box = lambda: asyncio.sleep(0, result=None)
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(p.click(locator))
    assert err.value.code == code
    assert code in pacing.TARGET_REFUSALS
    assert not [e for e in page.log if e[0] in ("move", "down", "up")]


def test_a_hidden_target_is_refused_before_any_scroll():
    """Scrolling a ``display:none`` element waits out its timeout on real Chrome, so a target
    that stays hidden is told as hidden before the scroll, which is never reached."""
    page = FakePage()
    p, _ = _pacer(page, action_timeout=0.2)
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(p.click(FakeLocator(page, visible=False, hang=True)))
    assert err.value.code == "not_visible"
    assert page.log == []


def test_a_target_a_transition_hides_for_a_moment_is_still_pressed():
    """A page in a transition shows its target again: the step goes on, as it did before."""
    page = FakePage()
    p, _ = _pacer(page)
    locator = FakeLocator(page, visible=False)
    looks = []

    async def shows_again():
        looks.append(1)
        return len(looks) > 3
    locator.is_visible = shows_again
    asyncio.run(p.click(locator))
    assert [e for e in page.log if e[0] == "down"]


def test_a_zero_area_target_says_no_size_and_a_hidden_one_does_not():
    """The visibility check says no to both; only the box tells them apart."""
    page = FakePage()
    p, _ = _pacer(page)
    for box, code in (((400, 300, 0, 0), "no_size"), ((400, 300, 100, 0), "no_size"),
                      ((400, 300, 100, 40), "not_visible")):
        with pytest.raises(pacing.ActionError) as err:
            asyncio.run(p.click(FakeLocator(page, visible=False, box=box)))
        assert err.value.code == code
    gone = FakeLocator(page, visible=False)
    gone.bounding_box = lambda: asyncio.sleep(0, result=None)
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(p.click(gone))
    assert err.value.code == "not_visible"


def test_a_target_the_scroll_hides_is_told_hidden():
    """The check after the scroll stays: a page can hide the target as it comes into view."""
    page = FakePage()
    p, _ = _pacer(page)
    locator = FakeLocator(page)

    async def hides(timeout=None):
        locator.visible = False
    locator.scroll_into_view_if_needed = hides
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(p.click(locator))
    assert err.value.code == "not_visible"


def test_a_hanging_keystroke_returns_control():
    """The click completes; the first key never does: the per-key bound returns control."""
    page = FakePage()
    entered = []

    async def hang(ch):
        entered.append(ch)
        await asyncio.sleep(3600)

    async def no_pause(_d):
        return None
    page.keyboard.type = hang
    p = pacing.Pacer(page, rng=random.Random(1), sleep=no_pause, action_timeout=0.2)

    async def bounded_by_the_test():  # without the per-key bound this fails in 3 s, not hangs
        return await asyncio.wait_for(p.type(FakeLocator(page), "abc"), 3)
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(bounded_by_the_test())
    assert err.value.code == "timeout"
    assert entered == ["a"]                        # reached the keystroke, then timed out


def test_a_hanging_wheel_step_returns_control():
    page = FakePage()

    async def hang(dx, dy):
        await asyncio.sleep(3600)

    async def no_pause(_d):
        return None
    page.mouse.wheel = hang
    p = pacing.Pacer(page, rng=random.Random(1), sleep=no_pause, action_timeout=0.2)

    async def bounded_by_the_test():
        return await asyncio.wait_for(p.scroll("down", "step"), 3)
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(bounded_by_the_test())
    assert err.value.code == "timeout"


def test_visible_click_timeout():
    """A target that never settles returns control within the fixed action timeout."""
    page = FakePage()
    p = pacing.Pacer(page, rng=random.Random(1), action_timeout=0.2)
    with pytest.raises(pacing.ActionError) as err:
        asyncio.run(p.click(FakeLocator(page, hang=True)))
    assert err.value.code == "timeout"
    assert pacing.ACTION_TIMEOUT == 5.0


def test_extension_hosts_are_exact_and_follow_the_account_region():
    from casa_browser import infra
    hosts = infra.onepassword_extension_hosts("acme.1password.eu")
    assert "acme.1password.eu" in hosts and "b5n.1password.eu" in hosts
    assert not any(h.endswith(".com") or "*" in h for h in hosts)
    assert infra.onepassword_extension_hosts(None) == frozenset()
    assert infra.onepassword_extension_hosts("acme.1password.com") >= {"my.1password.com"}


def test_wrapper_never_outlives_a_parent_that_died_before_the_death_signal():
    ppids = iter([4242, 1])                       # the driver dies between the two reads
    assert launcher.arm_parent_death(getppid=lambda: next(ppids), prctl=lambda *a: 0) is False
    assert launcher.arm_parent_death(getppid=lambda: 1, prctl=lambda *a: 0) is False
    assert launcher.arm_parent_death(getppid=lambda: 4242, prctl=lambda *a: -1) is False
    assert launcher.arm_parent_death(getppid=lambda: 4242, prctl=lambda *a: 0) is True


def test_second_launch_refused_while_one_runs(tmp_path):
    """Through the real handler: a launch while another holds the launcher is refused before
    anything is spawned."""
    server = launcher.Launcher(proc=_no_processes(), paths=launcher.LauncherPaths(profiles=tmp_path / "p", home=tmp_path / "h"),
                               controller_uid=os.getuid(), floor=lambda: True,
                               trust=lambda home: pytest.fail("must not get as far as trusting the CA"))
    assert server.acquire()
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pipes = [os.pipe() for _ in range(2)]
    fds = [pipes[0][1], pipes[0][1], pipes[1][0], pipes[0][1]]
    header = json.dumps({"op": "launch", "argv": ARGS, "env": {"CASA_BROWSER_PROFILE": "site:s1"}}).encode() + b"\n"
    socket.send_fds(a, [header], fds)
    t = threading.Thread(target=server.handle, args=(b,))
    t.start()
    t.join(10)
    a.settimeout(5)
    assert json.loads(a.recv(4096)) == {"ok": False, "error": "busy"}
    server.release()
    for r, w in pipes:
        os.close(r)
        os.close(w)


def test_descriptors_are_received_close_on_exec():
    """No window in which a passed descriptor is inheritable by a concurrent spawn."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    r, w = os.pipe()
    socket.send_fds(a, [b'{"op": "x"}\n'], [r])
    flags_seen = []
    real = b.recvmsg

    class Conn:
        def recvmsg(self, *args):
            flags_seen.append(args[2] if len(args) > 2 else 0)
            return real(*args)
    msg, fds = launcher.Launcher._receive(Conn())
    assert msg == {"op": "x"} and len(fds) == 1
    assert flags_seen and all(f & socket.MSG_CMSG_CLOEXEC for f in flags_seen)
    assert os.get_inheritable(fds[0]) is False
    for fd in (r, w, fds[0]):
        os.close(fd)


def test_launcher_start_reaps_a_leftover_chrome(tmp_path):
    proc = tmp_path / "proc"
    for pid, cmd, uid in [(100, b"/opt/google/chrome/chrome\0--type=renderer", 1100),
                          (101, b"/opt/google/chrome/chrome\0", 1103),      # another user's
                          (102, b"/usr/bin/python3\0", 1100),               # not Chrome
                          (103, b"/opt/google/chrome/chrome_crashpad_handler\0", 1100)]:
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(cmd)
        (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    killed = []
    assert sorted(launcher.reap_leftover_chrome(proc, uid=1100, kill=lambda p, s: killed.append(p))) == [100, 103]
    assert sorted(killed) == [100, 103]


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [{'ok': True}, {'ok': False}, None])
async def test_forced_browser_close_requires_launcher_confirmation(tmp_path, reply):
    """An unconfirmed launcher close retains the exact manager session."""
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    path = tmp_path / 'launch.sock'
    calls = []

    async def handle(reader, writer):
        calls.append(json.loads(await reader.readline()))
        if reply is not None:
            writer.write(json.dumps(reply).encode() + b'\n')
            await writer.drain()
        writer.close()

    listener = await asyncio.start_unix_server(handle, path=path)
    manager = browser.BrowserManager(placeholder_dir=tmp_path / 'placeholder', launcher_socket=path)
    context = SimpleNamespace(close=AsyncMock())
    session = browser.BrowserSession('account', 's1', context)
    manager.session = session
    try:
        if reply == {'ok': True}:
            await manager.close(session=session, force=True)
            assert manager.session is None
            context.close.assert_awaited_once()
        else:
            with pytest.raises((RuntimeError, ValueError)):
                await manager.close(session=session, force=True)
            assert manager.session is session
            context.close.assert_not_called()
        assert calls == [{'op': 'close'}]
        with pytest.raises(RuntimeError, match='session'):
            await manager.close(session=browser.BrowserSession('account', 'other', context), force=True)
        assert len(calls) == 1
    finally:
        listener.close()
        await listener.wait_closed()


def test_process_group_exit_ignores_only_exited_members(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    for pid, state, group in [(100, 'Z', 100), (101, 'S', 100), (102, 'S', 200)]:
        entry = proc / str(pid)
        entry.mkdir()
        (entry / 'stat').write_text(f'{pid} (chrome (renderer)) {state} 1 {group} 0')
    assert launcher.process_group_running(100, proc)
    (proc / '101' / 'stat').write_text('101 (chrome) Z 1 100 0')
    assert not launcher.process_group_running(100, proc)
    assert launcher.process_group_running(200, proc)


def test_launcher_close_waits_for_exit_confirmation(tmp_path, monkeypatch):
    """The authenticated control request cannot acknowledge a kill without exit proof."""
    server = launcher.Launcher(proc=_no_processes(), controller_uid=os.getuid())
    killed = threading.Event()
    server._pid = 12345
    server._stopped.clear()
    monkeypatch.setattr(launcher.os, 'killpg', lambda pid, sig: killed.set())
    a, b = socket.socketpair()
    a.settimeout(1)
    a.sendall(b'{"op":"close"}\n')
    t = threading.Thread(target=server.handle, args=(b,))
    t.start()
    try:
        assert killed.wait(1)
        import select
        assert not select.select([a], [], [], 0.02)[0]
        server._stopped.set()
        assert json.loads(a.recv(4096)) == {'ok': True}
    finally:
        server._stopped.set()
        t.join(1)
        a.close()
        b.close()


def test_launcher_close_refuses_unconfirmed_exit():
    from unittest.mock import Mock
    server = launcher.Launcher(proc=_no_processes(), controller_uid=os.getuid())
    server._stopped = Mock()
    server._stopped.wait.return_value = False
    with pytest.raises(LaunchRefused, match='unavailable'):
        server.close()
    server._stopped.wait.assert_called_once_with(5)


# --------------------------------------------------------------------------- exit proof


def _no_processes():
    """An empty synthetic process table: a unit test never reads or signals the real one."""
    import tempfile
    return Path(tempfile.mkdtemp(prefix="casa-proc-"))


def chrome_process(proc, pid=4242, state="D", group=4200):
    entry = proc / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "cmdline").write_bytes(b"/opt/google/chrome/chrome\0--type=renderer\0")
    (entry / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\n")
    (entry / "stat").write_text(f"{pid} (chrome) {state} 1 {group} 0")
    return entry / "stat"


@pytest.mark.parametrize("state", ["D", "S"])
def test_exited_leader_does_not_confirm_surviving_group(tmp_path, monkeypatch, state):
    proc = tmp_path / "proc"
    member = chrome_process(proc, state=state)
    leader_exited = threading.Event()

    def waitpid(pid, flags):
        leader_exited.set()
        return pid, 0

    replies = []
    conn = SimpleNamespace(recv=lambda _: b"", shutdown=lambda _: None,
                           sendall=replies.append)
    monkeypatch.setattr(launcher.os, "waitpid", waitpid)
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: None)
    clock = iter([0, 2])
    # The real sleep stays: the re-check thread the Launcher starts here sleeps between scans.
    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: next(clock),
                                                          sleep=time.sleep))
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc)
    try:
        assert control._supervise(conn, 4200) is False
        assert leader_exited.is_set()
        assert json.loads(replies[0]) == {"ok": False, "exit": 0}
    finally:
        # The surviving member exits, so the re-check thread proves the stop and ends here.
        # Left running it outlives the test and reads ``launcher.chrome_alive`` while a later
        # test has it patched (test_a_fork_during_the_first_empty_scan_is_seen_by_the_second).
        member.unlink()
        assert control._stopped.wait(2)


def test_unconfirmed_group_retains_lock_pid_and_profile_until_exit(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    cleanup = Mock()
    monkeypatch.setattr(launcher, "chrome_argv", lambda *args: ([str(launcher.CHROME)], cleanup))
    spawned = Mock(return_value=4200)
    monkeypatch.setattr(launcher.os, "posix_spawn", spawned)
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: None)
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc,
                                floor=lambda: True, trust=lambda _: None)
    assert control._stopped.wait(2)
    member = chrome_process(proc)  # the session's surviving group member
    monkeypatch.setattr(control, "_supervise", lambda conn, pid: False)
    fds = [os.open(os.devnull, os.O_RDWR) for _ in range(4)]
    try:
        control._launch(None, {}, fds)
        assert not fds
        assert control._pid == 4200
        assert not control._stopped.is_set()
        assert not control.acquire()
        with pytest.raises(launcher.LaunchRefused, match="busy"):
            control._launch(None, {}, [])
        spawned.assert_called_once()
        cleanup.assert_not_called()
        member.write_text("4242 (chrome) Z 1 4200 0")
        assert control._stopped.wait(2)
        control.close()
        assert control._pid is None
        cleanup.assert_called_once()
        assert control.acquire()
        control.release()
    finally:
        member.unlink(missing_ok=True)
        assert control._stopped.wait(2)
        for fd in fds:
            os.close(fd)


@pytest.mark.parametrize("signal_error", [None, PermissionError])
def test_inherited_process_blocks_initial_stop_and_launch(tmp_path, signal_error):
    proc = tmp_path / "proc"
    member = chrome_process(proc)
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        if signal_error:
            raise signal_error

    leftovers = launcher.reap_leftover_chrome(proc, kill=kill)
    assert leftovers == [4242]
    assert signals == [(4242, signal.SIGKILL)]
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc,
                                floor=lambda: True)
    try:
        assert not control._stopped.is_set()
        assert not control.acquire()
        with pytest.raises(launcher.LaunchRefused, match="busy"):
            control.close()
        with pytest.raises(launcher.LaunchRefused, match="busy"):
            control._launch(None, {}, [])
        with pytest.raises(launcher.LaunchRefused, match="busy"):
            control.forget("site")
        member.write_text("4242 (chrome) Z 1 4200 0")
        assert control._stopped.wait(2)
        control.close()
        assert control.acquire()
        control.release()
    finally:
        member.unlink(missing_ok=True)
        assert control._stopped.wait(2)


@pytest.mark.asyncio
async def test_inherited_uninterruptible_process_retains_driver_guards(tmp_path):
    proc = tmp_path / "proc"
    member = chrome_process(proc)
    leftovers = launcher.reap_leftover_chrome(proc, kill=lambda *args: None)
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc)
    sock = tmp_path / "launcher.sock"
    listener = control.serve(sock)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    # (on / remove_listener: a login session watches its page's frames, slice 5u)
    page = SimpleNamespace(is_closed=lambda: False, on=lambda *a: None,
                           remove_listener=lambda *a: None)
    context = SimpleNamespace(pages=[page], close=AsyncMock())
    manager = browser.BrowserManager(placeholder_dir=tmp_path / "placeholder", launcher_socket=sock)
    session = browser.BrowserSession("account", "site", context)
    manager.session = session
    proxy = SimpleNamespace(begin_login=AsyncMock(return_value=("boot", 1)),
                            end_login=AsyncMock(), close_window=AsyncMock(), revoke=AsyncMock())
    driver = login.DriverServer(manager, tmp_path / "driver.sock", cleanup_timeout=0.01)
    sid = await driver.open_session("login", "run", "site", session, proxy, ())
    held = driver._sessions[sid].lock
    await held.acquire()
    try:
        with pytest.raises(BrokerError, match="unavailable"):
            await driver.close_session(sid)
        assert launcher.process_group_running(4200, proc)
        assert session.login_active
        assert sid in driver._sessions
        assert manager.session is session
        proxy.revoke.assert_not_awaited()
        proxy.end_login.assert_not_awaited()
        proxy.close_window.assert_not_awaited()
        context.close.assert_not_awaited()
        member.write_text("4242 (chrome) Z 1 4200 0")
        assert control._stopped.wait(2)
        await driver.close_session(sid)
        assert not session.login_active
        assert sid not in driver._sessions
        assert manager.session is None
        proxy.revoke.assert_awaited_once_with("run")
        proxy.close_window.assert_not_awaited()
    finally:
        held.release()
        member.unlink(missing_ok=True)
        assert control._stopped.wait(2)
        listener.shutdown()
        listener.server_close()
        thread.join(2)


def test_unreadable_process_state_is_not_exit_proof(tmp_path):
    proc = tmp_path / "proc"
    member = chrome_process(proc)
    member.write_text("unreadable state")
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc)
    try:
        assert not control._stopped.is_set()
        assert not control.acquire()
        assert launcher.process_group_running(4200, proc)
        assert launcher.process_group_running(4200, tmp_path / "missing")
    finally:
        member.unlink()
        assert control._stopped.wait(2)


def test_launch_failure_before_spawn_releases_lock(tmp_path):
    control = launcher.Launcher(proc=_no_processes(), controller_uid=os.getuid(), floor=lambda: True)
    with pytest.raises(launcher.LaunchRefused, match="argv"):
        control._launch(None, {}, [])
    assert control._stopped.is_set()
    control.close()
    assert control.acquire()
    control.release()


def test_chrome_forked_after_the_startup_reap_still_blocks(tmp_path):
    """A Chrome child forked after the reaper's snapshot is Chrome of the browser user too:
    it blocks the stopped state, is killed again by the re-check, and only its exit releases."""
    proc = tmp_path / "proc"
    proc.mkdir()
    signals = []
    late = None
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc,
                                kill=lambda pid, sig: signals.append((pid, sig)))
    try:
        assert control._stopped.wait(2)          # nothing alive at start
        assert control.acquire()
        late = chrome_process(proc, pid=4343, state="S", group=4200)
        # The end of a session, as ``_launch`` ends one: not stopped while a Chrome lives.
        assert not control._release_when_stopped()
        assert (4343, signal.SIGKILL) in signals  # and it was killed again
        threading.Thread(target=control._recheck_exit, daemon=True).start()
        assert not control._stopped.wait(0.3)
        late.write_text("4343 (chrome) Z 1 4200 0")
        assert control._stopped.wait(2)
    finally:
        if late is not None:
            late.unlink(missing_ok=True)


def test_a_fork_during_the_first_empty_scan_is_seen_by_the_second(tmp_path, monkeypatch):
    """Stopped needs two consecutive empty scans: a renderer forked while the first scan ran
    (its parent exiting before the scan reached it) is seen by the second, killed, and keeps
    the launcher busy."""
    control = launcher.Launcher(proc=_no_processes(), controller_uid=os.getuid(),
                                kill=lambda pid, sig: signals.append((pid, sig)))
    assert control._stopped.wait(2)
    assert control.acquire()
    signals = []
    scans = iter([[], [4545], [], []])
    monkeypatch.setattr(launcher, "chrome_alive", lambda proc, uid: next(scans))
    assert not control._release_when_stopped()   # scan 1 empty, scan 2 sees the fork
    assert signals == [(4545, signal.SIGKILL)]
    assert control._release_when_stopped()       # two consecutive empty scans


def test_reused_pid_of_another_program_is_not_chrome(tmp_path):
    """A pid reused by an unrelated program never keeps the launcher busy."""
    proc = tmp_path / "proc"
    entry = proc / "4242"
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"/usr/bin/python3\0-m\0something\0")
    (entry / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\n")
    (entry / "stat").write_text("4242 (python3) S 1 4242 0")
    other = proc / "4343"
    other.mkdir()
    (other / "cmdline").write_bytes(b"/opt/google/chrome/chrome\0")
    (other / "status").write_text(f"Uid:\t{os.getuid() + 1}\t{os.getuid() + 1}\n")  # another user
    (other / "stat").write_text("4343 (chrome) S 1 4343 0")
    assert launcher.chrome_alive(proc) == []
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc)
    assert control._stopped.wait(2)
    assert control.acquire()
    control.release()


def test_only_the_real_process_table_is_signalled(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher.os, "kill", lambda *a: calls.append(a))
    proc = tmp_path / "proc"
    member = chrome_process(proc, state="S")
    control = launcher.Launcher(controller_uid=os.getuid(), proc=proc)
    assert not control._stopped.is_set()
    assert calls == []                           # a synthetic table is never signalled
    member.write_text("4242 (chrome) Z 1 4200 0")
    assert control._stopped.wait(2)


def test_unreadable_process_table_is_not_exit_proof(tmp_path):
    """A process table that cannot be read proves nothing: the launcher stays not stopped."""
    assert launcher.chrome_alive(tmp_path / "missing") is None
    control = launcher.Launcher(controller_uid=os.getuid(), proc=tmp_path / "missing")
    assert not control._stopped.is_set()
    assert not control.acquire()
    (tmp_path / "missing").mkdir()
    assert control._stopped.wait(2)


@pytest.mark.asyncio
async def test_next_generation_is_the_opened_sessions(tmp_path):
    """The run controller installs a session's policy before the browser starts, naming the
    generation ``open`` will give it (Task 12)."""
    class Page:
        def on(self, *a):
            pass

        def is_closed(self):
            return False

    async def launch(path, **kw):
        return SimpleNamespace(pages=[Page()], close=AsyncMock(), new_page=AsyncMock())
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    manager = browser.BrowserManager(placeholder_dir=tmp_path / "p", playwright=pw)
    for _ in range(3):
        expected = manager.next_generation
        session = await manager.open("site1", "account", EffectiveLocale("en", "UTC", "option"))
        assert session.generation == expected and manager.next_generation == expected + 1
