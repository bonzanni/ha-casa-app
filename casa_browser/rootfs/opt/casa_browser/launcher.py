"""The browser launcher (``svc-browser-launcher``, user ``casa-browser``; Plan 1 Task 8).

Chrome runs as the browser user, which owns the profiles; Patchright runs in the controller,
which owns the run state. The controller's Patchright launches ``bin/chrome-launch`` as its
"browser executable". That wrapper connects here and passes, with ``SCM_RIGHTS``, its stdout,
stderr and the two CDP pipe ends (fds 3 and 4), plus Patchright's argv and a small
environment. The launcher — unprivileged, as the browser user — checks them and starts Chrome
with those descriptors in a new session:

* the floor marker must exist (``netfloor.floor_installed``): no network floor, no browser;
* the profile comes from ``CASA_BROWSER_PROFILE`` (``site:<id>`` → ``/data/profiles/<id>``, or
  ``ephemeral`` → a fresh directory removed when Chrome exits), replacing Patchright's
  ``--user-data-dir``;
* ``--remote-debugging-pipe`` is required; a debugging port or address, ``--no-sandbox``,
  ``--headless``, extension loading and any page argument but ``about:blank`` are refused;
* Chrome's environment is an allowlist built here;
* one Chrome at a time; when the wrapper's connection closes (Patchright killed it, or the
  controller died) the whole Chrome session is killed, and when Chrome exits its status goes
  back to the wrapper.

Only the controller user may connect (``SO_PEERCRED``). ``forget`` deletes one site's profile.
"""
from __future__ import annotations

import array
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from casa_browser import netfloor
from casa_browser.protocol import ID_PATTERN

CHROME = Path("/opt/google/chrome/chrome")
SOCKET = Path("/run/casa-browser/launcher/launch.sock")
CA_PUBLIC = Path("/run/casa-browser/ca/ca.pem")
DISPLAY = ":99"
XAUTHORITY = Path("/run/casa-browser/x/Xauthority")
MAX_HEADER = 256 * 1024
_TZ = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}")

# Chrome's argv is an ALLOWLIST: exactly the switches Patchright 1.64.1 and
# ``browser.launch_options`` emit (captured from a real launch), with fixed values where a
# value matters. Anything else — a sandbox, debugging, extension or proxy switch it does not
# list — is refused, so no caller can weaken the launch.
ALLOWED_FLAGS = frozenset({
    "--disable-field-trial-config", "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows", "--disable-breakpad", "--no-default-browser-check",
    "--disable-dev-shm-usage", "--disable-edgeupdater", "--disable-hang-monitor",
    "--disable-prompt-on-repost", "--disable-renderer-backgrounding", "--disable-updater-scheduler",
    "--no-first-run", "--use-mock-keychain", "--no-service-autorun", "--export-tagged-pdf",
    "--disable-search-engine-choice-screen", "--edge-skip-compat-layer-relaunch",
    "--disable-infobars", "--disable-sync", "--remote-debugging-pipe",
    "--hide-crash-restore-bubble",
})
FIXED_VALUES = {
    "--force-color-profile": {"srgb"},
    "--password-store": {"basic"},
    "--disable-blink-features": {"AutomationControlled"},
    "--proxy-server": {"http://127.0.0.1:3128"},
    "--proxy-bypass-list": {"<-loopback>"},
    "--window-position": {"0,0"},
    "--window-size": {"1920,1080"},
    "--use-angle": {"vulkan"},
}
# Feature lists: every item must be one of these (order and subset free).
FEATURE_LISTS = {
    "--disable-features": frozenset({
        "AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose",
        "DialMediaRouteProvider", "GlobalMediaControls", "HttpsUpgrades", "LensOverlay",
        "MediaRouter", "PaintHolding", "ThirdPartyStoragePartitioning",
        "BlockOriginHeaderModificationOnRedirect", "AvoidCorsURLLoaderRestartOnRedirect",
        "Translate", "AutoDeElevate", "OptimizationHints", "NetworkTimeServiceQuerying",
        "AimEnabled", "msForceBrowserSignIn", "msEdgeUpdateLaunchServicesPreferredVersion"}),
    "--enable-features": frozenset({"CDPScreenshotNewSurface", "Vulkan", "DefaultANGLEVulkan",
                                    "VulkanFromANGLE"}),
}
REQUIRED_ARGS = ("--remote-debugging-pipe", "--proxy-server=http://127.0.0.1:3128",
                 "--proxy-bypass-list=<-loopback>")
_LANG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")


def _allowed(arg: str) -> bool:
    if arg in ALLOWED_FLAGS:
        return True
    name, sep, value = arg.partition("=")
    if not sep:
        return False
    if name in FIXED_VALUES:
        return value in FIXED_VALUES[name]
    if name in FEATURE_LISTS:
        items = value.split(",")
        return bool(value) and all(i in FEATURE_LISTS[name] for i in items)
    if name == "--lang":
        return _LANG.fullmatch(value) is not None
    return False


class LaunchRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LauncherPaths:
    profiles: Path = Path("/data/profiles")
    home: Path = Path("/run/casa-browser/browser-home")


def _profile_dir(paths: LauncherPaths, profile) -> tuple[Path, Callable[[], None] | None]:
    if profile == "ephemeral":
        base = paths.home / "ephemeral"
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(tempfile.mkdtemp(prefix="p-", dir=base))
        return path, lambda: shutil.rmtree(path, ignore_errors=True)
    if not isinstance(profile, str) or not profile.startswith("site:"):
        raise LaunchRefused("profile")
    site_id = profile[len("site:"):]
    if not ID_PATTERN.fullmatch(site_id):
        raise LaunchRefused("profile")
    paths.profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = paths.profiles / site_id
    if path.is_symlink():
        raise LaunchRefused("profile")
    path.mkdir(exist_ok=True, mode=0o700)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise LaunchRefused("profile")
    os.chmod(path, 0o700)
    return path, None


def chrome_argv(argv: list, profile, paths: LauncherPaths) -> tuple[list[str], Callable | None]:
    """Check Patchright's argv and return Chrome's, with the real profile directory."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise LaunchRefused("argv")
    args = argv[1:]
    # Required, each exactly once: the pipe as the only debugging channel, the enforcing proxy
    # and the removal of Chrome's implicit loopback bypass.
    for required in REQUIRED_ARGS:
        if args.count(required) != 1:
            raise LaunchRefused("argv")
    if sum(a.startswith("--user-data-dir=") for a in args) != 1:
        raise LaunchRefused("argv")
    out = []
    for a in args:
        if a.startswith("--user-data-dir="):
            continue  # replaced by the real profile below
        if a != "about:blank" and not _allowed(a):
            raise LaunchRefused("argv")
        out.append(a)
    path, cleanup = _profile_dir(paths, profile)
    return [str(CHROME), f"--user-data-dir={path}", *out], cleanup


def chrome_env(client_env: dict, home: Path) -> dict:
    tz = client_env.get("TZ")
    if not isinstance(tz, str) or not _TZ.fullmatch(tz) or ".." in tz:
        tz = "UTC"
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "DISPLAY": DISPLAY,
            "XAUTHORITY": str(XAUTHORITY), "TZ": tz, "LANG": "C.UTF-8"}


def trust_ca(home: Path, ca: Path = CA_PUBLIC, run=subprocess.run) -> None:
    """Chrome on Linux trusts the NSS database in ``$HOME/.pki/nssdb`` (measured, S0)."""
    db = home / ".pki" / "nssdb"
    db.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (db / "cert9.db").exists():
        run(["certutil", "-d", f"sql:{db}", "-N", "--empty-password"], check=True,
            capture_output=True, timeout=30)
    run(["certutil", "-d", f"sql:{db}", "-D", "-n", "casa-browser-proxy"], capture_output=True,
        timeout=30)
    run(["certutil", "-d", f"sql:{db}", "-A", "-t", "C,,", "-n", "casa-browser-proxy", "-i", str(ca)],
        check=True, capture_output=True, timeout=30)


def process_group_running(pgid: int, proc: Path = Path("/proc")) -> bool:
    """Zombies have exited and cannot execute a queued page action."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            return True  # Unreadable process state is not exit confirmation.
    return False


EXIT_SCAN_GAP = 0.05  # seconds between the two empty scans that prove Chrome stopped


def chrome_alive(proc: Path = Path("/proc"), uid: int | None = None) -> list[int] | None:
    """Every live Chrome process of ``uid`` — the launcher's one exit proof.

    A process counts when it belongs to ``uid``, its command line is a program in Chrome's
    directory and it is not a zombie (a zombie has exited and cannot run a queued page action).
    This covers a session's whole process group, processes left by an earlier launcher and a
    child forked after any snapshot, and a reused pid of another program is never Chrome.
    Unreadable state of a process of ``uid`` counts as alive; an unreadable ``proc`` → None."""
    uid = os.getuid() if uid is None else uid
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    prefix = str(CHROME.parent).encode() + b"/"
    alive = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            owner = int((entry / "status").read_text().split("Uid:")[1].split()[0])
        except FileNotFoundError:
            continue
        except (OSError, IndexError, ValueError):
            alive.append(int(entry.name))  # cannot tell whose it is: not exit proof
            continue
        if owner != uid:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            continue
        except (OSError, IndexError, ValueError):
            alive.append(int(entry.name))
            continue
        if state in ("Z", "X"):
            continue
        if cmd.startswith(prefix) or not cmd:  # an empty command line: exiting, not yet a zombie
            alive.append(int(entry.name))
    return alive


class Launcher:
    def __init__(self, *, paths: LauncherPaths = LauncherPaths(), controller_uid: int,
                 floor: Callable[[], bool] = netfloor.floor_installed, trust=trust_ca,
                 proc: Path = Path("/proc"), uid: int | None = None, kill=None):
        self.paths = paths
        self.controller_uid = controller_uid
        self.floor = floor
        self.trust = trust
        self._busy = threading.Lock()
        self._process_lock = threading.Lock()
        self._pid = None
        self._proc = proc
        self._uid = os.getuid() if uid is None else uid
        # Only the real process table is ever signalled (tests pass a synthetic one).
        self._kill = kill if kill is not None else (
            os.kill if proc == Path("/proc") else (lambda pid, sig: None))
        self._stopped = threading.Event()
        self._busy.acquire()
        if not self._release_when_stopped():
            threading.Thread(target=self._recheck_exit, daemon=True).start()

    def check_floor(self) -> None:
        if not self.floor():
            raise LaunchRefused("no_floor")

    def acquire(self) -> bool:
        with self._process_lock:
            acquired = self._busy.acquire(blocking=False)
            if acquired:
                self._stopped.clear()
            return acquired

    def release(self) -> None:
        if not self._release_when_stopped():
            raise LaunchRefused("busy")

    def _chrome_running(self, pid: int | None = None) -> bool:
        """True while any Chrome process of the browser user lives (``pid``, the session's
        leader, is covered by the same scan); each one found is killed again, so a process
        forked after an earlier kill does not outlive the check. Stopped needs two
        consecutive empty scans ``EXIT_SCAN_GAP`` apart: a process forked while the first
        scan ran (its parent exiting before the scan reached it) is seen by the second."""
        for scan in range(2):
            if scan:
                time.sleep(EXIT_SCAN_GAP)
            alive = chrome_alive(self._proc, self._uid)
            if alive is None:
                return True
            for found in alive:
                try:
                    self._kill(found, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if alive:
                return True
        return False

    def _release_when_stopped(self, cleanup=None) -> bool:
        """One exit proof gates close acknowledgement, profile cleanup and the next launch."""
        with self._process_lock:
            if self._chrome_running(self._pid):
                return False
            if cleanup is not None:
                cleanup()
            self._pid = None
            self._stopped.set()
            self._busy.release()
            return True

    def _recheck_exit(self, cleanup=None) -> None:
        # Each attempt is a single process-state scan. RPC waits stay bounded even when a
        # killed process remains in uninterruptible sleep; eventual exit releases the lock.
        while not self._release_when_stopped(cleanup):
            time.sleep(0.1)

    def forget(self, site_id) -> None:
        if not isinstance(site_id, str) or not ID_PATTERN.fullmatch(site_id):
            raise LaunchRefused("profile")
        if not self.acquire():  # never while a Chrome may hold the profile open
            raise LaunchRefused("busy")
        try:
            path = self.paths.profiles / site_id
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        finally:
            self.release()

    def close(self) -> None:
        """Kill Chrome's process group and acknowledge only its confirmed exit."""
        with self._process_lock:
            if self._pid is not None:
                try:
                    os.killpg(self._pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self._busy.locked():
                raise LaunchRefused("busy")
        if not self._stopped.wait(5):
            raise LaunchRefused("unavailable")

    # ------------------------------------------------------------------ one connection

    def handle(self, conn: socket.socket) -> None:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if int.from_bytes(creds[4:8], sys.byteorder, signed=True) != self.controller_uid:
            self._reply(conn, {"ok": False, "error": "not_controller"})
            return
        msg, fds = self._receive(conn)
        try:
            if not isinstance(msg, dict):
                raise LaunchRefused("unsupported")
            if msg == {"op": "close"} and not fds:
                self.close()
                self._reply(conn, {"ok": True})
                return
            if msg.get("op") == "forget":
                self.forget(msg.get("site_id"))
                self._reply(conn, {"ok": True})
                return
            if msg.get("op") != "launch" or len(fds) != 4:
                raise LaunchRefused("unsupported")
            self._launch(conn, msg, fds)
        except LaunchRefused as err:
            self._reply(conn, {"ok": False, "error": err.code})
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    @staticmethod
    def _receive(conn) -> tuple[object, list[int]]:
        """The header line and the passed descriptors, received close-on-exec: with
        ``MSG_CMSG_CLOEXEC`` there is no moment in which a concurrent spawn could inherit them
        (``socket.recv_fds`` ignores its flags, so it is not used)."""
        data = b""
        fds: list[int] = []
        int_size = array.array("i").itemsize
        while not data.endswith(b"\n"):
            chunk, ancdata, _flags, _addr = conn.recvmsg(
                65536, socket.CMSG_LEN(8 * int_size), socket.MSG_CMSG_CLOEXEC)
            for level, kind, payload in ancdata:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    got = array.array("i")
                    got.frombytes(payload[:len(payload) - len(payload) % int_size])
                    fds.extend(got)
            if not chunk or len(data) + len(chunk) > MAX_HEADER:
                return None, fds
            data += chunk
        try:
            return json.loads(data), fds
        except ValueError:
            return None, fds

    @staticmethod
    def _reply(conn, payload) -> None:
        try:
            conn.sendall(json.dumps(payload).encode() + b"\n")
        except OSError:
            pass

    def _launch(self, conn, msg, fds) -> None:
        self.check_floor()
        if not self.acquire():
            raise LaunchRefused("busy")
        cleanup = None
        try:
            env_in = msg.get("env") if isinstance(msg.get("env"), dict) else {}
            argv, cleanup = chrome_argv(msg.get("argv"), env_in.get("CASA_BROWSER_PROFILE"), self.paths)
            env = chrome_env(env_in, self.paths.home)
            self.trust(self.paths.home)
            out, err, cdp_in, cdp_out = fds
            pid = os.posix_spawn(argv[0], argv, env, file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
                (os.POSIX_SPAWN_DUP2, out, 1), (os.POSIX_SPAWN_DUP2, err, 2),
                (os.POSIX_SPAWN_DUP2, cdp_in, 3), (os.POSIX_SPAWN_DUP2, cdp_out, 4),
            ], setsid=True)
            with self._process_lock:
                self._pid = pid
                self._stopped.clear()
            # Chrome holds the descriptors now. Close ours (once: ``handle`` sees an empty
            # list), or the CDP pipe would never reach end-of-file when Chrome exits.
            for fd in fds:
                os.close(fd)
            fds.clear()
            self._supervise(conn, pid)
        finally:
            if not self._release_when_stopped(cleanup):
                threading.Thread(target=self._recheck_exit, args=(cleanup,), daemon=True).start()

    def _supervise(self, conn, pid: int) -> bool:
        """Wait for Chrome's exit or the wrapper's disconnect, whichever comes first."""
        exited = threading.Event()
        status = {}

        def wait():
            _, st = os.waitpid(pid, 0)
            status["code"] = os.waitstatus_to_exitcode(st)
            exited.set()
            try:
                conn.shutdown(socket.SHUT_RD)  # wakes the reader below
            except OSError:
                pass

        threading.Thread(target=wait, daemon=True).start()
        try:
            while not exited.is_set():
                if not conn.recv(1):
                    break
        except OSError:
            pass
        try:
            os.killpg(pid, signal.SIGKILL)  # the whole session: browser, zygotes, renderers
        except (ProcessLookupError, PermissionError):
            pass
        stopped = exited.wait(3)
        deadline = time.monotonic() + 1
        while stopped and self._chrome_running(pid):
            if time.monotonic() >= deadline:
                stopped = False
                break
            time.sleep(0.01)
        self._reply(conn, {"ok": stopped, "exit": status.get("code", -9)})
        return stopped

    def serve(self, path: Path = SOCKET):
        launcher = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                launcher.handle(self.request)

        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o666)  # SO_PEERCRED is the check
        return server


# --------------------------------------------------------------------------- the wrapper


def arm_parent_death(getppid=os.getppid, prctl=None) -> bool:
    """Die with the parent (Patchright's driver). False when that cannot be armed, or when the
    parent already died before it was: the wrapper must then not hand Chrome anything."""
    if prctl is None:
        import ctypes
        prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
    parent = getppid()
    if parent == 1 or prctl(1, signal.SIGKILL) != 0:  # PR_SET_PDEATHSIG
        return False
    return getppid() == parent


def client_main(argv: list[str], socket_path: Path = SOCKET) -> int:  # pragma: no cover — image test
    """``bin/chrome-launch``: hand fds 1–4, argv and the profile to the launcher; exit with
    Chrome's status. Dies with its parent (Patchright's driver)."""
    if not arm_parent_death():
        return 1
    env = {k: os.environ[k] for k in ("CASA_BROWSER_PROFILE", "TZ") if k in os.environ}
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(socket_path))
    header = json.dumps({"op": "launch", "argv": argv, "env": env}).encode() + b"\n"
    socket.send_fds(s, [header], [1, 2, 3, 4])
    for fd in (3, 4):
        os.close(fd)  # Chrome holds them now; Patchright sees EOF when Chrome exits
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            return 1
        data += chunk
    reply = json.loads(data)
    if not reply.get("ok"):
        print(f"casa-browser launcher refused: {reply.get('error')}", file=sys.stderr)
        return 1
    return int(reply.get("exit", 1)) & 0xFF


def reap_leftover_chrome(proc: Path = Path("/proc"), uid: int | None = None,
                         kill=os.kill) -> list[int]:
    """Signal leftover Chrome processes and return every pid requiring exit confirmation."""
    uid = os.getuid() if uid is None else uid
    killed = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
            owner = int((entry / "status").read_text().split("Uid:")[1].split()[0])
        except (OSError, IndexError, ValueError):
            continue
        if owner == uid and cmd.startswith(str(CHROME.parent).encode() + b"/"):
            killed.append(int(entry.name))
            try:
                kill(int(entry.name), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return killed


def main() -> int:  # pragma: no cover — the svc-browser-launcher service
    import pwd
    controller = pwd.getpwnam("casa-controller").pw_uid
    reap_leftover_chrome()  # the launcher then reports stopped only once none of them lives
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    server = Launcher(controller_uid=controller).serve()
    try:
        os.write(3, b"\n")  # s6 readiness
        os.close(3)
    except OSError:
        pass
    server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
