"""The controller's Chrome sessions (Plan 1 Task 8).

``BrowserManager`` opens one browser session at a time: an account site's persistent profile,
or a fresh ephemeral profile for the public session (open-web runs and an account run's public
grants). A switch closes the previous Chrome completely before the next starts, so two
sessions never share a process, a cookie jar or a window. Chrome is branded Google Chrome,
headed under the virtual display, with the Chromium sandbox, the browser's own viewport, no
custom user agent or headers, and every request through the enforcing proxy with the implicit
loopback bypass removed.

Patchright runs here, in the controller; Chrome itself runs as the browser user, started by
``launcher.py`` through ``bin/chrome-launch``. The profile is named, never given as a path:
the launcher maps ``site:<id>`` and ``ephemeral`` to directories only it can read.

The launch measurements this builds on (Plan 1 S0): ``chromium_sandbox=True`` (Patchright
otherwise adds ``--no-sandbox``); ``--disable-background-networking`` dropped from
Patchright's defaults (otherwise the policy-installed extension never downloads); the Vulkan
ANGLE flags (otherwise rendering is software under Xvfb).
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path

from casa_browser.protocol import ID_PATTERN, EffectiveLocale

CHROME_WRAPPER = Path("/opt/casa_browser/bin/chrome-launch")
PROXY_SERVER = "http://127.0.0.1:3128"
# Chrome keeps only the LAST --enable-features, so this one repeats Patchright's own
# (CDPScreenshotNewSurface) together with the Vulkan ANGLE features.
GPU_FLAGS = ("--use-angle=vulkan",
             "--enable-features=CDPScreenshotNewSurface,Vulkan,DefaultANGLEVulkan,VulkanFromANGLE")
SCREEN = (1920, 1080)
LAUNCH_TIMEOUT_MS = 90_000
# Where Chrome (the browser user) writes a file it downloads; the controller reads it from
# there (``downloads.Downloader.capture_browser_download``). Made by root bootstrap.
DOWNLOADS = Path("/data/downloads")


def profile_for(kind: str, site_id: str | None) -> str:
    if kind == "account" and isinstance(site_id, str) and ID_PATTERN.fullmatch(site_id):
        return f"site:{site_id}"
    if kind == "public" and site_id is None:
        return "ephemeral"
    raise ValueError("session")


def launch_options(*, profile: str, locale: EffectiveLocale, gpu: bool = True,
                   downloads: Path = DOWNLOADS) -> dict:
    args = [f"--proxy-server={PROXY_SERVER}", "--proxy-bypass-list=<-loopback>",
            f"--lang={locale.language}", "--window-position=0,0",
            f"--window-size={SCREEN[0]},{SCREEN[1]}",
            # After a forced close: no "Restore pages?" bubble. A tab Chrome restores while
            # Patchright is attached never finishes loading (measured on Chrome 151).
            "--hide-crash-restore-bubble"]
    if gpu:
        args.extend(GPU_FLAGS)
    return {
        "executable_path": str(CHROME_WRAPPER),
        "headless": False,
        "no_viewport": True,
        "service_workers": "allow",
        "chromium_sandbox": True,
        "ignore_default_args": ["--disable-background-networking"],
        "args": args,
        "env": {"CASA_BROWSER_PROFILE": profile, "TZ": locale.timezone},
        "timeout": LAUNCH_TIMEOUT_MS,
        "accept_downloads": True,
        "downloads_path": str(downloads),
    }


@dataclass(eq=False)
class BrowserSession:
    """A live session. Holds Patchright objects; never serialised or logged."""

    kind: str
    site_id: str | None
    context: object = field(repr=False)
    generation: int = 1
    # Advances on every navigation of any frame of the page (a new document, a client-side
    # route, a fragment change): observations, handles and screenshots are bound to it.
    view_generation: int = 0
    login_active: bool = False  # set while a broker login holds the page (Task 9)

    @property
    def page(self):
        pages = [p for p in self.context.pages if not p.is_closed()]
        return pages[0] if pages else None

    def __repr__(self) -> str:
        return f"BrowserSession(kind={self.kind!r}, generation={self.generation})"


class BrowserManager:
    def __init__(self, *, placeholder_dir: Path, playwright=None, gpu: bool = True,
                 launcher_socket: Path | None = None, downloads: Path = DOWNLOADS):
        self.placeholder_dir = placeholder_dir
        self.downloads = downloads
        self._playwright = playwright
        self._owned = None
        self.gpu = gpu
        self.launcher_socket = launcher_socket
        self.session: BrowserSession | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._views = itertools.count(1)  # one counter across sessions: no generation repeats

    @property
    def next_generation(self) -> int:
        """The generation the next ``open`` gives its session (the run controller installs
        the session's policy before the browser starts)."""
        return self._generation + 1

    async def _pw(self):
        if self._playwright is None:
            from patchright.async_api import async_playwright
            self._owned = await async_playwright().start()
            self._playwright = self._owned
        return self._playwright

    async def open(self, site_id: str | None, mode: str, locale: EffectiveLocale) -> BrowserSession:
        """Close whatever runs, then start the session for ``site_id`` (``None``: the public
        session; an open-web run always uses it)."""
        kind = "public" if mode == "open_web" or site_id is None else "account"
        profile = profile_for(kind, None if kind == "public" else site_id)
        async with self._lock:
            await self._close_locked()
            self.placeholder_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            pw = await self._pw()
            context = await pw.chromium.launch_persistent_context(
                str(self.placeholder_dir), **launch_options(profile=profile, locale=locale, gpu=self.gpu,
                                                         downloads=self.downloads))
            if not context.pages:
                await context.new_page()
            self._generation += 1
            session = BrowserSession(kind, None if kind == "public" else site_id, context,
                                     self._generation, next(self._views))

            def moved(_frame, session=session):
                session.view_generation = next(self._views)

            context.pages[0].on("framenavigated", moved)
            self.session = session
            return session

    async def _close_locked(self) -> None:
        session, self.session = self.session, None
        if session is not None:
            try:
                await asyncio.wait_for(session.context.close(), 30)
            except Exception:  # a dead browser is closed already; the launcher kills the rest
                pass

    async def close(self, *, session: BrowserSession | None = None, force: bool = False) -> None:
        async with self._lock:
            if session is not None and self.session is not session:
                raise RuntimeError("session")
            if force:
                # Only the launcher can confirm that the browser user's Chrome stopped.
                from casa_browser import launcher
                import json
                reader, writer = await asyncio.open_unix_connection(
                    str(self.launcher_socket or launcher.SOCKET))
                try:
                    writer.write(b'{"op":"close"}\n')
                    await writer.drain()
                    reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
                    if reply != {"ok": True}:
                        raise RuntimeError("close unconfirmed")
                finally:
                    writer.close()
            await self._close_locked()

    async def forget(self, site_id: str) -> None:
        """Close the browser if it is on ``site_id`` (any session holds the launcher), then
        have the launcher delete that site's profile — and only that one."""
        from casa_browser import launcher
        async with self._lock:
            await self._close_locked()
            reader, writer = await asyncio.open_unix_connection(
                str(self.launcher_socket or launcher.SOCKET))
            try:
                import json
                writer.write(json.dumps({"op": "forget", "site_id": site_id}).encode() + b"\n")
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), 30))
            finally:
                writer.close()
            if not reply.get("ok"):
                raise RuntimeError("forget refused")

    async def shutdown(self) -> None:
        await self.close()
        if self._owned is not None:
            await self._owned.stop()
            self._owned = self._playwright = None
