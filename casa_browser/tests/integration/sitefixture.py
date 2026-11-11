"""Synthetic fixture sites for the Chrome tests in the app image.

The production proxy refuses private destinations, so a fixture site on loopback can only be
reached through the test harness proxy (``proxyharness.Harness``: the production addon and
mitmproxy setup, with a test resolver and loop at the harness boundary). ``ChromeSites``
stops the production proxy service (``s6-svc -d``; the launcher stays up), runs the harness on
the same port as root in a background thread, and makes its CA the one the launcher trusts.
Everything is restored on exit.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import proxyharness
from casa_browser.infra import onepassword_extension_hosts
from casa_browser.proxy_addon import WINDOW_QUIET
from casa_browser.proxy_client import CONTROL_SOCKET

CA_PUBLIC = Path("/run/casa-browser/ca/ca.pem")
PROXY_SERVICE = "/run/service/svc-browser-proxy"


def _port_listening(port: int = 3128) -> bool:
    needle = f":{port:04X} "
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(table).read_text().splitlines()[1:]:
                parts = line.split()
                if parts[1].endswith(needle.strip()) and parts[3] == "0A":
                    return True
        except OSError:
            pass
    return False


def _wait_port_free(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while _port_listening() or _port_listening(3130):
        if time.monotonic() > deadline:
            raise RuntimeError("port 3128 or 3130 still in use")
        time.sleep(0.2)


class _Harness(proxyharness.Harness):
    """The harness on the production listener ports: the browser's (3128) and the
    controller's downloader's (3130, S4-5), so the image's controller fetches through it."""

    def _make_proxy(self):
        super()._make_proxy()
        from casa_browser import netfloor
        port = netfloor.PROXY_DOWNLOAD_PORT
        self.download_port = self.addon.download_port = self.proxy.download_port = port


class ChromeSites:
    def __init__(self, hostnames: list[str]):
        self.hostnames = hostnames
        self.tmp = Path(tempfile.mkdtemp(prefix="casa-it-sites-"))
        self.loop = None
        self.harness = None
        self._ready = threading.Event()
        self._stop = None

    def __enter__(self):
        # Every log record the harness proxy (production addon code, in this process) makes
        # is kept, through the production proxy's own filter (``proxy_main._Withhold``: a
        # library record keeps no text), at every level — what the proxy service would write
        # with ``log_level: debug`` — so a test's secret scan covers that stream too.
        import logging
        from casa_browser.proxy_main import _Withhold
        self.log_records = []
        self._log_handler = logging.Handler()
        self._log_handler.addFilter(_Withhold())
        self._log_handler.emit = lambda record: self.log_records.append(
            f"{record.name} {record.levelname} {record.getMessage()}")
        self._root_level = logging.getLogger().level
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.DEBUG)
        subprocess.run(["/command/s6-svc", "-wd", "-T", "10000", "-d", PROXY_SERVICE], check=True)
        _wait_port_free()
        shutil.copyfile(CA_PUBLIC, self.tmp / "production-ca.pem")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(30):
            raise RuntimeError("harness did not start")
        # Every test starts outside the credential window, with events that keep their origin:
        # a test that looks for an origin's absence would otherwise pass for the wrong reason.
        if self.call(lambda: self.harness.addon.window_open):
            raise RuntimeError("the harness proxy's credential window is still open")
        shutil.copyfile(self.tmp / "ca" / "mitmproxy-ca-cert.pem", CA_PUBLIC)
        return self

    def _run(self):
        with asyncio.Runner(loop_factory=proxyharness.FixtureLoop) as runner:
            runner.run(self._serve())

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        # The controller uses the production control path, just as Chrome uses port 3128.
        async with _Harness(self.tmp, self.hostnames, browser_port=3128,
                                       control_path=CONTROL_SOCKET,
                                       account_hosts=frozenset(h for h in self.hostnames
                                           if h in onepassword_extension_hosts(
                                               'acct.1password.eu'))) as hz:
            self.harness = hz
            # As the controller does before a run's first browser: a started proxy opens the
            # credential window; no Chrome has run yet, so close it.
            hz.addon.close_window(hz.addon.token)
            # That close takes effect WINDOW_QUIET later, a quiet that covers requests begun
            # while the window was open. Nothing has connected to this proxy yet, so end the
            # quiet now instead of waiting it out: the state is the one the wait would reach.
            # The quiet itself is covered by the addon's unit tests (test_proxy_adapter.py).
            hz.addon.window_until -= WINDOW_QUIET
            self._ready.set()
            await self._stop.wait()

    def call(self, fn, *args):
        """Run ``fn(*args)`` on the harness loop (addon state is loop-owned)."""
        async def go():
            return fn(*args)
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(10)

    def install(self, policy):
        revision = self.call(self.harness.addon.install, policy)
        # An install closes browser connections, which moves a closing credential window's end
        # 3 s on (credential-window rule 2): wait it out so the test's events are raw again.
        until = self.call(lambda: self.harness.addon.window_until)
        now = self.call(lambda: self.harness.addon.clock())
        if until != float('inf') and until > now:
            time.sleep(until - now + 0.05)
        return revision

    @property
    def receipts(self):
        return self.harness.fixture.receipts

    @property
    def pages(self):
        return self.harness.fixture.pages

    @property
    def status(self):
        return self.harness.fixture.status

    def events(self):
        return self.call(lambda: list(self.harness.addon.events))

    def __exit__(self, *exc):
        import logging
        logging.getLogger().removeHandler(self._log_handler)
        logging.getLogger().setLevel(self._root_level)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(30)
        _wait_port_free()  # the harness released the port: the service can bind it again
        shutil.copyfile(self.tmp / "production-ca.pem", CA_PUBLIC)
        subprocess.run(["/command/s6-svc", "-wU", "-T", "30000", "-u", PROXY_SERVICE], check=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
