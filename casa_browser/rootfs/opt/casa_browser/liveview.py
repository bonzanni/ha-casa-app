"""The live view (Plan 1 Task 13, operator pick "W+"): grants, claims, the operator's viewer
sessions, its two sockets and everything it does to Chrome on his behalf.

``LiveView`` is the run controller's ``grants`` (it replaces Task 12's minimal issuer). It keeps,
in controller memory only (a restart interrupts every run anyway):

* **Grants** — one per run, issued by ``request_live``: 120 s to claim, single use.
* **Sessions** — one per run, created only by the ``expose`` callback ``RunController.begin_live``
  calls once the manual lease is held and, for a login/assist view, the broker acknowledged the
  capture. No session, no socket, no input: nothing of the operator reaches Chrome earlier.

**Who is the viewer.** Opening a view sets an ``HttpOnly; SameSite=Strict`` cookie whose hash the
session keeps; every viewer page served with that cookie gets a fresh random *viewer id*, and the
page carries it on both sockets. The page holding the current id is the one viewer: an older page
(another tab, the page before a reload or a second Open) is refused on upgrade, and a message it
queued earlier is dropped, because every input operation re-checks its socket's viewer id.

**The screen lock.** Everything done to Chrome for the view — the window fit, input, the field
probe, the selection poll — runs under one lock and first checks, under it, that its session is
still the run's, still ``viewing`` and its viewer current. Every end of the view first changes the
phase (so nothing new starts), closes the sockets, then takes the lock (so the one operation in
flight finishes first), and only then restores the window. Each operation is bounded in time.

**Window (W+).** The X screen stays 1920×1080. During the view only Chrome's window is fitted to
the viewer (``Browser.setWindowBounds``; Chrome's windows are at least 500 px wide, so a narrower
phone scales the picture); the restore puts it back to 1920×1080 at 0,0 and is verified by reading
the bounds and the page's ``screen`` back.

**What the probe sees** (editable-field rectangles, types, a non-password field's value, a password
field's length only, window geometry, host and title) goes only to the authenticated viewer. It is
not an observation: never logged, stored or shown to a model. Nothing this module handles is logged
beyond an exception's type.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from casa_browser.protocol import LiveGrant, ProtocolError

log = logging.getLogger("casa_browser.liveview")

GRANT_S = 120
SCREEN = (1920, 1080)
MIN_WINDOW = (500, 300)
OP_TIMEOUT_S = 5.0
PROBE_S = 0.3
SELECTION_S = 0.7
PING_S = 5.0
MAX_TEXT = 256          # per message (the viewer splits longer text): typed within the bound
INJECT_TIMEOUT_S = 10.0
MAX_SELECTION = 200
SELECTALL_QUIET_S = 2.0  # a selection read this soon after the viewer's SelectAll is not offered
MAX_NOTCHES = 40
MAX_REPLACED = 8        # earlier devices' cookies remembered per view, for their cover text
MAX_REFUSED = 256       # refused sends told in every state until the viewer read them (it
                        # keeps at most 64 unresolved)
VNC_SOCKET = Path("/run/casa-browser/vnc/vnc.sock")
CLOSE_ENDED, CLOSE_ELSEWHERE, CLOSE_DONE = 4001, 4002, 4003
KEYS = {"Enter": "Return", "Tab": "Tab", "ShiftTab": "shift+Tab", "Backspace": "BackSpace",
        "Escape": "Escape", "Delete": "Delete",
        # the strip's caret buttons (live view K, revision 7)
        "Left": "Left", "Right": "Right",
        # the phone's delete-word key (live view K, plain keyboard)
        "CtrlBackspace": "ctrl+BackSpace",
        # the address bar's text replaced by what the operator typed (casa-test F13)
        "SelectAll": "ctrl+a"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(16).replace("=", "")


class LiveRefused(Exception):
    """A claim or Open that cannot be served (the code is a fixed word for the page)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(eq=False)
class Grant:
    """Single use: every claim of it, successful or not, removes it."""

    grant_id: str
    run_id: str
    purpose: str
    expires_at: datetime


@dataclass(eq=False)
class Session:
    run_id: str
    purpose: str
    token_sha256: str = field(repr=False)
    # The cookies of devices another Open took the view from (casa-test F12): their pages are
    # told it was opened elsewhere, not that it ended. Bounded; never a way in.
    replaced: list = field(default_factory=list, repr=False)
    viewer_id: str | None = field(default=None, repr=False)
    phase: str = "viewing"                 # viewing | review | ended
    ctl: dict = field(default_factory=dict, repr=False)   # ws -> viewer id
    vnc: dict = field(default_factory=dict, repr=False)   # ws -> viewer id
    poke: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    loop: asyncio.Task | None = field(default=None, repr=False)
    state: dict | None = field(default=None, repr=False)  # the last probe (window geometry)
    resend: bool = False      # the next probe's state goes out even when unchanged
    quiet_selection_until: float = 0.0   # the viewer's own SelectAll: its selection is no offer
    ack: int = 0              # the last viewer message number taken (``q``), for its states
    refused: list = field(default_factory=list)   # texts (``q``) not typed, until the viewer's ``ok``
    unsure: list = field(default_factory=list)    # texts whose typing failed part-way, likewise


def _clamp(value, low, high) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


# --------------------------------------------------------------------------- the screen


# The editable fields of a document, open shadow roots included, in document order — one walker
# for the probe and the guard. Slice 5g: date and time fields are fields too (the phone's keys
# reach their segments: digits, A/P, ← → between segments, Backspace); a range or colour is not. Each field gets an id the first time it is walked: an
# expando on its wrapper in this isolated world (Patchright makes the world once per document
# and reuses it; the page's own scripts never see it), so an id names one element for its life —
# a replaced field is a new id, a removed one is found by nobody.
_WALK = r"""
  const BAD = new Set(['button','submit','reset','checkbox','radio','file','image','range','color',
    'hidden']);
  const editable = el => {
    if (el.isContentEditable && (!el.parentElement || !el.parentElement.isContentEditable)) return true;
    if (el.tagName === 'TEXTAREA') return !el.disabled && !el.readOnly;
    if (el.tagName === 'INPUT') return !BAD.has((el.type || 'text').toLowerCase()) && !el.disabled && !el.readOnly;
    return false; };
  const list = []; let pw = false;
  const walk = root => { for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) walk(el.shadowRoot);
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password') pw = true;
    if (editable(el)) list.push(el); } };
  walk(document);
  const reg = globalThis.__casaFields || (globalThis.__casaFields = {next: 1});
  const idOf = el => el.__casaId || (el.__casaId = reg.next++);
  const deep = () => { let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a; };
  const docId = Math.round(performance.timeOrigin * 1000);
"""

# The field probe, evaluated in each frame's isolated world (Patchright's default). A password
# field's value is never read: only its length.
PROBE = "() => {" + _WALK + r"""
  const a = deep();
  const label = el => ((el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
    || el.getAttribute('placeholder') || el.getAttribute('name') || '') + '').trim().slice(0, 120);
  const fields = [];
  list.forEach(el => {
    const r = el.getBoundingClientRect();
    // The focused field is always kept, wherever it is: a window shrunk for the phone's keyboard
    // leaves it below the viewport until reveal scrolls it in (casa-test F31).
    const keep = el === a;
    if (!keep && (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth)) return;
    const cs = getComputedStyle(el); if (!keep && (cs.visibility === 'hidden' || cs.display === 'none')) return;
    const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase()
      : (el.tagName === 'TEXTAREA' ? 'textarea' : 'contenteditable');
    const secret = type === 'password';
    // A field ever seen as a password field stays one for the phone's keyboard (first sight).
    if (secret) el.__casaPw = true;
    const pwlike = !!el.__casaPw || /password/i.test(el.getAttribute('autocomplete') || '');
    const len = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? el.value.length : (el.textContent || '').length;
    fields.push({id: idOf(el), x: r.left, y: r.top, w: r.width, h: r.height, type, im: el.inputMode || '',
      ac: secret ? '' : (el.getAttribute('autocomplete') || ''), ml: el.maxLength > 0 ? el.maxLength : 0,
      focused: el === a, len, lb: label(el), pw: pwlike,
      val: secret ? null : (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? el.value : (el.textContent || '')).slice(0, 256)});
  });
  return {fields, iw: innerWidth, ih: innerHeight, sx: screenX, sy: screenY, ow: outerWidth, oh: outerHeight,
    doc: docId, host: location.host, title: document.title.slice(0, 120),
    frame: !!(a && (a.tagName === 'IFRAME' || a.tagName === 'FRAME')), pw};
}"""

# Whether a probed field (by document and id) is its document's focused element, still editable.
IS_FOCUSED = "([id, doc]) => {" + _WALK + r"""
  const d = deep();
  return docId === doc && !!d && d.__casaId === id && list.includes(d);
}"""

READ_BACK = "() => [screen.width, screen.height, outerWidth, outerHeight]"


class ChromeScreen:
    """Chrome's window and the operator's input, for the controller (uid 1103): CDP on the
    controller's own Patchright connection for the window and the probe, ``xdotool`` for input
    at the X level (the layer a VNC pointer event lands on), ``xclip`` for the X selection."""

    def __init__(self, *, xdotool: str = "xdotool", xclip: str = "xclip"):
        self.xdotool = xdotool
        self.xclip = xclip
        self._cdp: dict = {}
        self._launch: dict = {}   # (session generation, window) -> bounds before the view
        self._docs: dict = {}     # last probe: (frame index, document) -> (frame, screen offset)

    @staticmethod
    def _pages(bs) -> list:
        try:
            return [p for p in bs.context.pages if not p.is_closed()]
        except Exception:  # noqa: BLE001
            return []

    def page(self, bs):
        """The page the operator sees: the most recently opened open page."""
        pages = self._pages(bs)
        return pages[-1] if pages else None

    async def _window(self, bs, page):
        entry = self._cdp.get(id(page))
        if entry is None or entry[0] is not page:
            cdp = await bs.context.new_cdp_session(page)
            window = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
            entry = (page, cdp, window)
            self._cdp = {k: v for k, v in self._cdp.items() if not v[0].is_closed()}
            self._cdp[id(page)] = entry
        return entry[1], entry[2]

    async def _read_bounds(self, cdp, window) -> tuple:
        b = (await cdp.send("Browser.getWindowBounds", {"windowId": window}))["bounds"]
        return (b.get("left"), b.get("top"), b.get("width"), b.get("height"))

    async def _bounds(self, bs, page, left, top, width, height) -> None:
        cdp, window = await self._window(bs, page)
        key = (getattr(bs, "generation", None), window)
        if key not in self._launch:
            self._launch[key] = await self._read_bounds(cdp, window)
        await cdp.send("Browser.setWindowBounds", {"windowId": window,
                                                   "bounds": {"windowState": "normal"}})
        await cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {
            "left": left, "top": top, "width": width, "height": height}})

    async def fit(self, bs, width: int, height: int) -> None:
        page = self.page(bs)
        if page is not None:
            await self._bounds(bs, page, 0, 0, _clamp(width, MIN_WINDOW[0], SCREEN[0]),
                               _clamp(height, MIN_WINDOW[1], SCREEN[1]))

    async def restore(self, bs) -> bool:
        """Every window the view resized back to the bounds it had before (Chrome as launched:
        ``--window-size=1920,1080`` at 0,0 on the 1920×1080 screen, which Chrome without a window
        manager makes 1919×1079 — measured), verified by reading back the bounds and each page's
        ``screen`` (1920×1080, the X screen the view never changes). No page left: nothing to
        restore (a new Chrome starts as launched)."""
        pages = self._pages(bs)
        seen = set()
        for page in pages:
            cdp, window = await self._window(bs, page)
            key = (getattr(bs, "generation", None), window)
            if window not in seen and key in self._launch:
                seen.add(window)
                await self._bounds(bs, page, *self._launch[key])
        for page in pages:
            cdp, window = await self._window(bs, page)
            expected = self._launch.get((getattr(bs, "generation", None), window))
            bounds = await self._read_bounds(cdp, window)
            if expected is not None and bounds != expected:
                return False
            if expected is None and (bounds[2] or 0) < SCREEN[0] - 1:
                return False      # a window the view never resized must still be full width
            screen = await page.evaluate(READ_BACK)
            if list(screen[:2]) != list(SCREEN):
                return False
        return True

    async def probe(self, bs) -> dict | None:
        page = self.page(bs)
        if page is None:
            return None
        main = await page.evaluate(PROBE)
        border = max(0, (main["ow"] - main["iw"]) // 2)
        ox = main["sx"] + border
        oy = main["sy"] + main["oh"] - main["ih"] - border
        docs = {(0, main["doc"]): (page.main_frame, ox, oy)}
        # The focus chain: a frame holds the focus only if its parent does and its element is
        # the parent's focused element — only the end of the chain has a focused field (Astra
        # d1: two sibling frames each kept a focused field of their own).
        chain = {page.main_frame: bool(main.get("frame"))}   # frame -> focus goes deeper
        fields = [dict(f, fi=0, doc=main["doc"], x=f["x"] + ox, y=f["y"] + oy,
                       focused=f["focused"] and not main.get("frame")) for f in main["fields"]]
        password = bool(main.get("pw"))
        unchecked = False   # a frame whose password fields could not be looked for
        for index, frame in enumerate(page.frames[1:], start=1):
            # Every frame is looked at for password fields, hidden or not; only a visible
            # one contributes field rectangles.
            try:
                sub = await frame.evaluate(PROBE)
            except Exception:  # noqa: BLE001 — a frame that navigates or detaches
                unchecked = True
                continue
            password = password or bool(sub.get("pw"))
            box = active = None
            try:
                element = await frame.frame_element()
                box = await element.bounding_box()
                active = chain.get(frame.parent_frame) and await element.evaluate(IS_ACTIVE)
            except Exception:  # noqa: BLE001
                pass
            if active:
                chain[frame] = bool(sub.get("frame"))
            if not box:
                continue
            dx, dy = box["x"] + ox, box["y"] + oy
            docs[(index, sub["doc"])] = (frame, dx, dy)
            for f in sub["fields"]:
                fields.append(dict(f, fi=index, doc=sub["doc"], x=f["x"] + dx, y=f["y"] + dy,
                                   focused=bool(f["focused"] and active and not sub.get("frame"))))
        self._docs = docs
        return {"win": [main["sx"], main["sy"], main["ow"], main["oh"]], "origin": [ox, oy],
                "doc": main["doc"],
                "view": [main["iw"], main["ih"]], "host": main["host"], "title": main["title"],
                "url": page.url, "fields": fields, "password": password, "unchecked": unchecked,
                "focus_in_frame": bool(main.get("frame"))}

    async def focused_is(self, bs, to: dict) -> bool:
        """Whether Chrome's keys go, right now, to the probed field ``to`` (frame index,
        document, id). The field's page must be the page the operator sees; it is brought to the
        front first (Chrome's keys then go to it — the same page ``inject`` types into), then
        every frame on the way down must be its parent's focused element and the field its
        document's. Only the last probe's frames resolve a key."""
        entry = self._docs.get((to["fi"], to["doc"]))
        if entry is None:
            return False
        frame = entry[0]
        if frame.is_detached() or frame.page is not self.page(bs):
            return False
        await frame.page.bring_to_front()
        node = frame
        while node.parent_frame is not None:
            element = await node.frame_element()
            if not await element.evaluate(IS_ACTIVE):
                return False
            node = node.parent_frame
        return bool(await frame.evaluate(IS_FOCUSED, [to["id"], to["doc"]]))

    async def _run(self, *args: str, stdin: bytes | None = None) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await proc.communicate(stdin)
        except BaseException:
            # A timed-out or cut operation must not go on typing outside the screen lock:
            # stop this child (only ever the xdotool/xclip process just started here).
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        return out or b""

    async def xdo(self, *args) -> None:
        await self._run(self.xdotool, *map(str, args))

    async def inject(self, bs, op: dict) -> None:
        t, x, y = op["t"], op.get("x"), op.get("y")
        if t == "tap":
            await self.xdo("mousemove", x, y, "click", 1)
            return
        page = self.page(bs)
        key = op.get("to")
        if key is not None:
            # Keys for a field go to that field's own page, brought to the front (CDP
            # Page.bringToFront → WebContents::Focus) — the page the guard just checked, not
            # whichever is newest by now.
            entry = self._docs.get((key["fi"], key["doc"]))
            if entry is None:
                return
            await entry[0].page.bring_to_front()
        if t == "scroll":
            n = op["n"]
            await self.xdo("mousemove", x, y, "click", "--repeat", abs(n), "--delay", 8,
                           5 if n > 0 else 4)
        elif t == "down":
            await self.xdo("mousemove", x, y, "mousedown", 1)
        elif t == "move":
            await self.xdo("mousemove", x, y)
        elif t == "up":
            await self.xdo("mousemove", x, y, "mouseup", 1)
        elif t == "text":
            await self.xdo("type", "--delay", 12, "--", op["s"])
        elif t == "key":
            await self.xdo("key", KEYS[op["k"]])
        elif t == "reveal":
            if page is not None:
                await reveal(page)

    async def selection(self) -> str:
        out = await self._run(self.xclip, "-o", "-selection", "primary")
        return out.decode("utf-8", "replace").strip()

    async def clear_selection(self) -> None:
        # xclip -i forks a child that serves the (empty) selection until another owner takes it;
        # its output stays unconnected so only the parent is waited for.
        proc = await asyncio.create_subprocess_exec(
            self.xclip, "-selection", "primary", "-i", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate(b"")


# The operator's focused field into sight (the viewer asks after its keyboard took the room);
# it reads nothing back. Run in the trusted driver's isolated world.
REVEAL = """() => {
    let e = document.activeElement;
    while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
    if (e && e !== document.body && e.scrollIntoView) e.scrollIntoView({block: 'center', inline: 'nearest'});
    return !!e && (e.tagName === 'IFRAME' || e.tagName === 'FRAME');
}"""
# Whether a frame's element is where the focus is, following open shadow roots as REVEAL does
# (an iframe inside a shadow root: the document's activeElement is the shadow host, r8 S2).
IS_ACTIVE = """e => {
    let a = document.activeElement;
    while (a && a !== e && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a === e;
}"""


async def reveal(page, depth: int = 5) -> None:
    """Bring the focused element into view, following the focus into frames: each document on
    the way scrolls its own focused element (a frame's element in its parent, then the field in
    the frame), so a field inside an iframe ends in sight too (r5 S2)."""
    frame = page.main_frame
    for _ in range(depth):
        if not await frame.evaluate(REVEAL):
            return
        child = None
        for f in frame.child_frames:
            try:
                element = await f.frame_element()
                if await element.evaluate(IS_ACTIVE):
                    child = f
                    break
            except Exception:  # noqa: BLE001 — a frame that went away
                continue
        if child is None:
            return
        frame = child


# --------------------------------------------------------------------------- messages


MAX_DOC = 2 ** 53       # a document id: performance.timeOrigin in microseconds
MAX_FIELD = 2 ** 31
MAX_FRAME = 1_000


def _int(value, low: int, high: int) -> int | None:
    """An integer in ``[low, high)`` as sent, else ``None`` (never clamped: it names something)."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value < high:
        return None
    return value


def _field_key(value) -> dict | None:
    """A probed field's key — frame index, document, id — or ``None``."""
    if not isinstance(value, dict):
        return None
    fi, doc, ident = (_int(value.get("fi"), 0, MAX_FRAME), _int(value.get("doc"), 0, MAX_DOC),
                  _int(value.get("id"), 1, MAX_FIELD))
    if fi is None or doc is None or ident is None:
        return None
    return {"fi": fi, "doc": doc, "id": ident}


def message_number(message: Any) -> int | None:
    """The viewer's counter on a message (``q``), acknowledged in every later state."""
    return _int(message.get("q"), 0, MAX_DOC) if isinstance(message, dict) else None


def parse_input(message: Any, state: dict | None) -> dict | None:
    """A viewer message as one of the fixed operations, its coordinates clamped to Chrome's
    window as last probed; ``None`` (dropped) for anything else or before any probe. Text is
    typed only into a checked element: a ``text`` names its field
    (``to``) and runs only while Chrome's focus is that field; the one exception is text for
    Chrome's address bar (``bar``), which the page cannot see. Any other text is dropped. A
    ``key`` may name its field too."""
    if not isinstance(message, dict) or state is None:
        return None
    t = message.get("t")
    if t == "fit":
        return {"t": "fit", "w": _clamp(message.get("w"), MIN_WINDOW[0], SCREEN[0]),
                "h": _clamp(message.get("h"), MIN_WINDOW[1], SCREEN[1])}
    if t == "reveal":
        return {"t": "reveal"}
    if t not in ("tap", "scroll", "down", "move", "up", "text", "key"):
        return None
    sx, sy, ow, oh = state["win"]
    op = {"t": t}
    if t in ("tap", "scroll", "down", "move", "up"):
        op["x"] = _clamp(message.get("x"), max(0, sx), max(0, sx) + max(1, ow) - 1)
        op["y"] = _clamp(message.get("y"), max(0, sy), max(0, sy) + max(1, oh) - 1)
    if t in ("text", "key") and message.get("to") is not None:
        to = _field_key(message["to"])
        if to is None:
            return None
        op["to"] = to
    if t == "text" and "to" not in op and message.get("bar") is not True:
        return None     # text goes only to a checked field, or to Chrome's address bar
    if t == "scroll":
        op["n"] = _clamp(message.get("n"), -MAX_NOTCHES, MAX_NOTCHES)
        if not op["n"]:
            return None
    if t == "text":
        s = message.get("s", "")
        if not isinstance(s, str) or not s or len(s) > MAX_TEXT:
            return None
        op["s"] = s
    if t == "key":
        if message.get("k") not in KEYS:
            return None
        op["k"] = message["k"]
    return op


def selection_offerable(probe: dict | None) -> bool:
    """Whether the X selection may be offered to the viewer as a copy: never from a page that
    holds a typed password — a password field with a value among the probed fields, focused or
    not (a Tab into a filled one selects its value, which Chrome puts in the X selection — found
    by the image lane; the page can move the focus away again before any look, r12/r13) — nor
    while the focus is in a frame whose focused field the probe could not see, nor without a
    probe."""
    if not probe:
        return False
    fields = probe.get("fields", ())
    if any(f.get("type") == "password" and f.get("len") for f in fields):
        return False
    if probe.get("focus_in_frame") and not any(f.get("focused") and f.get("fi") for f in fields):
        return False
    return True


# What the viewer gets of a field: where it is, what kind, its label, whether Chrome focuses it,
# and its key — never a value (nothing on the phone mirrors a field). viewer_state adds ``tx``:
# whether it holds any text at all (the strip's ← → show only then), a boolean and nothing more.
VIEWER_FIELD = ("x", "y", "w", "h", "type", "im", "ac", "focused", "lb", "doc", "fi", "id", "pw")


def viewer_state(probe: dict, ack: int = 0, refused: tuple = (), unsure: tuple = ()) -> dict:
    """What the viewer gets of a probe: geometry and fields without any value; a password
    field's autocomplete hint is dropped too. ``ack``: the last viewer message (``q``) taken
    before this probe; ``refused``: the texts (``q``) not typed — Chrome's focus was not their
    field, or they were dropped; ``unsure``: those whose typing failed part-way. The outcome of
    every send travels in the states, not in a reply that can be lost."""
    fields = []
    for f in probe.get("fields", ()):
        has = bool(f.get("len"))
        f = {k: f.get(k) for k in VIEWER_FIELD}
        f["tx"] = has          # whether it holds any text (the strip's arrows) — never how much
        if f.get("type") == "password":
            f["ac"] = ""
        fields.append({k: (round(v) if isinstance(v, float) else v) for k, v in f.items()})
    return {"t": "state", "win": [round(v) for v in probe["win"]],
            "origin": [round(v) for v in probe["origin"]], "view": probe["view"],
            "doc": probe.get("doc"), "ack": ack, "refused": list(refused), "unsure": list(unsure),
            "host": probe.get("host", ""), "fields": fields}


# --------------------------------------------------------------------------- the service


class LiveView:
    def __init__(self, *, store, browser, screen=None, clock: Callable[[], datetime] = _utcnow,
                 vnc_socket: Path = VNC_SOCKET):
        self.store = store
        self.browser = browser
        self.screen = screen if screen is not None else ChromeScreen()
        self.clock = clock
        self.vnc_path = vnc_socket
        self.controller = None
        self.grants: dict[str, Grant] = {}
        self.sessions: dict[str, Session] = {}
        self._claim_lock = asyncio.Lock()
        self._screen = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def attach(self, controller) -> None:
        self.controller = controller

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    # ------------------------------------------------------------------ grants (controller)

    def issue(self, run_id: str, purpose: str, now: datetime | None = None) -> LiveGrant:
        grant = Grant(_new_id("g"), run_id, purpose, (now or self.clock()) + timedelta(seconds=GRANT_S))
        self.grants[run_id] = grant
        return LiveGrant(grant.grant_id, grant.expires_at, f"live/{grant.grant_id}")

    def revoke(self, run_id: str) -> None:
        """Synchronous part of every end: no grant, the phase ``ended`` (nothing new starts),
        the sockets closing. ``end`` also waits for the operation in flight."""
        self.grants.pop(run_id, None)
        session = self.sessions.pop(run_id, None)
        if session is not None:
            session.phase = "ended"
            self._close_sockets(session, CLOSE_ENDED)

    def revoke_all(self) -> None:
        for run_id in list(self.grants) + list(self.sessions):
            self.revoke(run_id)

    async def end(self, run_id: str) -> None:
        self.revoke(run_id)
        async with self._screen:      # the one operation in flight has finished
            pass

    async def end_all(self) -> None:
        self.revoke_all()
        async with self._screen:
            pass

    async def close_view(self, run_id: str) -> bool:
        """Done: no more input or picture, then the window back to 1920×1080, verified. True
        when there is nothing to restore or the restore is verified."""
        session = self.sessions.get(run_id)
        if session is None:
            async with self._screen:
                return True
        if session.phase == "viewing":
            session.phase = "review"
        self._close_sockets(session, CLOSE_DONE)
        async with self._screen:
            bs = self.browser.session
            if bs is None:
                return True
            try:
                return bool(await asyncio.wait_for(self.screen.restore(bs), OP_TIMEOUT_S))
            except Exception as err:  # noqa: BLE001
                log.warning("window restore failed (%s)", type(err).__name__)
                return False

    # ------------------------------------------------------------------ claim and Open (UI)

    def _check_run(self, run_id: str, state: str) -> None:
        run = self.store.get_run(run_id)
        if run is None or run.state != state:
            raise LiveRefused("not_waiting")

    async def open_from_home(self, run_id: str) -> str:
        """The home page's Open: reconnects an established view (a new cookie, the old viewer
        refused), else claims the run's grant, expired or not (Open is the operator's own
        action through authenticated ingress; the grant's id is spent either way)."""
        async with self._claim_lock:
            session = self.sessions.get(run_id)
            if session is not None and session.phase in ("viewing", "review"):
                token = secrets.token_urlsafe(32)
                session.replaced = [*session.replaced, session.token_sha256][-MAX_REPLACED:]
                session.token_sha256 = _sha(token)
                await self._retire_viewer(session, None)
                return token
            self._check_run(run_id, "waiting_live")
            grant = self.grants.get(run_id)
            if grant is None:
                raise LiveRefused("not_waiting")
            return await self._claim(grant)

    async def claim(self, run_id: str, grant_id: str) -> str:
        """A grant's own page: only that run's unexpired, unclaimed grant; never a rebind."""
        async with self._claim_lock:
            grant = self.grants.get(run_id)
            if (grant is None or not hmac.compare_digest(grant.grant_id, grant_id)
                    or grant.expires_at <= self.clock()):
                raise LiveRefused("grant_refused")
            self._check_run(run_id, "waiting_live")
            return await self._claim(grant)

    def grant_run(self, grant_id: str) -> str | None:
        for grant in self.grants.values():
            if hmac.compare_digest(grant.grant_id, grant_id):
                return grant.run_id
        return None

    async def _claim(self, grant: Grant) -> str:
        del self.grants[grant.run_id]       # spent now, whatever the claim's outcome
        token = secrets.token_urlsafe(32)
        made: list[Session] = []

        async def expose():
            session = Session(grant.run_id, grant.purpose, _sha(token))
            self.sessions[grant.run_id] = session
            made.append(session)

        try:
            await self.controller.begin_live(grant.run_id, expose)
        except ProtocolError:
            raise LiveRefused("unavailable") from None
        if not made or self.sessions.get(grant.run_id) is not made[0]:
            raise LiveRefused("unavailable")
        return token

    # ------------------------------------------------------------------ the viewer page (UI)

    def _session_for(self, run_id: str, cookie: str | None) -> Session | None:
        session = self.sessions.get(run_id)
        if session is None or not cookie:
            return None
        if not hmac.compare_digest(_sha(cookie), session.token_sha256):
            return None
        return session

    async def _retire_viewer(self, session: Session, new_viewer: str | None) -> None:
        """A change of the current viewer ends the old one's view exactly as an end does: the
        new id at once (nothing new of the old page starts), its sockets closed, then the screen
        lock taken, so the old page's operation in flight finishes before this returns."""
        session.viewer_id = new_viewer
        self._close_sockets(session, CLOSE_ELSEWHERE)
        async with self._screen:
            pass

    async def serve_viewer(self, run_id: str, cookie: str | None) -> tuple[str, str | None]:
        """A viewer page: ``("viewing", new viewer id)`` (any older page stops being the viewer),
        ``("review", None)`` once Done was pressed, else ``("refused", None)``."""
        session = self._session_for(run_id, cookie)
        if session is None:
            return "refused", None
        if session.phase == "review":
            return "review", None
        if session.phase != "viewing" or not self._live(run_id):
            return "refused", None
        viewer = _new_id("v")
        await self._retire_viewer(session, viewer)
        return "viewing", viewer

    def is_viewer(self, run_id: str, cookie: str | None, viewer_id: str | None) -> bool:
        """Whether ``viewer_id`` is the current viewer of the cookie's view, still viewing."""
        session = self._session_for(run_id, cookie)
        return (session is not None and session.phase == "viewing" and bool(viewer_id)
                and session.viewer_id is not None
                and hmac.compare_digest(viewer_id, session.viewer_id))

    def view_state(self, run_id: str, cookie: str | None, viewer_id: str | None) -> str:
        session = self._session_for(run_id, cookie)
        if session is None:
            other = self.sessions.get(run_id)
            if (other is not None and other.phase in ("viewing", "review") and cookie
                    and any(hmac.compare_digest(_sha(cookie), old) for old in other.replaced)):
                return "elsewhere"     # another device's Open took this view over
            return "ended"
        if session.phase == "ended":
            return "ended"
        if session.phase == "review":
            return "review"
        if not viewer_id or session.viewer_id is None or \
                not hmac.compare_digest(viewer_id, session.viewer_id):
            return "elsewhere"
        return "viewing" if self._live(run_id) else "ended"

    def _live(self, run_id: str) -> bool:
        run = self.store.get_run(run_id)
        return run is not None and run.state == "live"

    def authorize(self, run_id: str, cookie: str | None, viewer_id: str | None) -> Session | None:
        """A socket upgrade: the cookie's session, its current viewer, ``viewing``, run ``live``."""
        session = self._session_for(run_id, cookie)
        if (session is None or session.phase != "viewing" or not viewer_id
                or session.viewer_id is None
                or not hmac.compare_digest(viewer_id, session.viewer_id) or not self._live(run_id)):
            return None
        return session

    def _current(self, session: Session, viewer_id: str) -> bool:
        return (self.sessions.get(session.run_id) is session and session.phase == "viewing"
                and session.viewer_id == viewer_id and self._live(session.run_id))

    # ------------------------------------------------------------------ sockets

    def _close_sockets(self, session: Session, code: int, keep=None) -> None:
        for table in (session.ctl, session.vnc):
            for ws in list(table):
                if ws is keep:
                    continue
                table.pop(ws, None)
                self._spawn(_close_ws(ws, code))

    async def ctl_socket(self, ws, session: Session, viewer_id: str) -> None:
        """The input and field channel of a prepared WebSocket, until it closes."""
        if not self._current(session, viewer_id):    # the upgrade's viewer was replaced meanwhile
            await _close_ws(ws, CLOSE_ELSEWHERE)
            return
        for other in [w for w in session.ctl if w is not ws]:
            session.ctl.pop(other, None)
            self._spawn(_close_ws(other, CLOSE_ELSEWHERE))
        session.ctl[ws] = viewer_id
        session.resend = True          # a new socket gets the current state, changed or not
        session.refused, session.unsure = [], []   # its page gives back what the old one left unresolved
        if session.loop is None or session.loop.done():
            session.loop = self._spawn(self._session_loop(session))
        session.poke.set()
        try:
            async for msg in ws:
                if getattr(msg, "type", None) is None or msg.type.name != "TEXT":
                    continue
                if len(msg.data) > 16 * MAX_TEXT:
                    continue
                try:
                    message = json.loads(msg.data)
                except ValueError:
                    continue
                await self._input(session, ws, message)
        finally:
            session.ctl.pop(ws, None)

    async def vnc_socket(self, ws, session: Session, viewer_id: str) -> None:
        """The picture: bytes between the WebSocket and x11vnc's view-only Unix socket, only
        while this socket's viewer is the session's current one (checked before every frame)."""
        if not self._current(session, viewer_id):
            await _close_ws(ws, CLOSE_ELSEWHERE)
            return
        for other in [w for w in session.vnc if w is not ws]:
            session.vnc.pop(other, None)
            self._spawn(_close_ws(other, CLOSE_ELSEWHERE))
        session.vnc[ws] = viewer_id
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.vnc_path))
        except OSError:
            session.vnc.pop(ws, None)
            await _close_ws(ws, 1011)
            return

        async def down():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data or session.vnc.get(ws) != viewer_id or \
                            not self._current(session, viewer_id):
                        break
                    await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                pass
            await _close_ws(ws, 1000)

        task = asyncio.ensure_future(down())
        try:
            async for msg in ws:
                if msg.type.name == "BINARY" and session.vnc.get(ws) == viewer_id:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type.name in ("CLOSE", "CLOSING", "CLOSED", "ERROR"):
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            session.vnc.pop(ws, None)
            task.cancel()
            writer.close()

    async def _send(self, session: Session, message: dict) -> None:
        data = json.dumps(message)
        for ws, viewer in list(session.ctl.items()):
            if viewer != session.viewer_id:
                continue
            try:
                await ws.send_str(data)
            except Exception:  # noqa: BLE001
                session.ctl.pop(ws, None)

    # ------------------------------------------------------------------ input

    async def _input(self, session: Session, ws, message) -> None:
        viewer_id = session.ctl.get(ws)
        if viewer_id is None:
            return
        async with self._screen:
            if not self._current(session, viewer_id) or session.ctl.get(ws) != viewer_id:
                return            # a message of a page that is no longer the viewer
            outcome = "refused"
            try:
                outcome = await self._apply(session, ws, viewer_id, message)
            finally:
                # Taken under the lock: a state probed after this carries it, with the outcome
                # of a text that was not surely typed.
                number = message_number(message)
                if number is not None:
                    session.ack = number
                    seen = _int(message.get("ok"), 0, MAX_DOC)
                    if seen is not None:      # the viewer has read the outcomes up to ``ok``
                        session.refused = [q for q in session.refused if q > seen]
                        session.unsure = [q for q in session.unsure if q > seen]
                    if outcome == "refused" and isinstance(message, dict) and message.get("t") in ("text", "key"):
                        session.refused = [*session.refused, number][-MAX_REFUSED:]
                    elif outcome == "unsure":
                        session.unsure = [*session.unsure, number][-MAX_REFUSED:]
        session.poke.set()

    async def _apply(self, session: Session, ws, viewer_id: str, message) -> str | None:
        """One message, under the screen lock. Its outcome: ``None`` (done, or nothing to type),
        ``"refused"`` (nothing typed: dropped, no browser, or meant for a field that is not
        Chrome's focus any more) or ``"unsure"`` (the typing failed part-way). The states tell
        a text's outcome; the viewer gets the text back unless it was typed."""
        bs = self.browser.session
        op = parse_input(message, session.state)
        if bs is None or op is None:
            return "refused"
        try:
            if op["t"] == "fit":
                await asyncio.wait_for(self.screen.fit(bs, op["w"], op["h"]), OP_TIMEOUT_S)
                return None
            if op.get("to") is not None:
                try:
                    here = await asyncio.wait_for(self.screen.focused_is(bs, op["to"]), OP_TIMEOUT_S)
                except Exception:  # noqa: BLE001 — a frame gone mid-check: not that field
                    here = False
                if not here:
                    return "refused"
        except Exception as err:  # noqa: BLE001 — never the message's content
            log.warning("live-view input failed (%s)", type(err).__name__)
            return "refused"
        try:
            await asyncio.wait_for(self.screen.inject(bs, op), INJECT_TIMEOUT_S)
        except Exception as err:  # noqa: BLE001 — never the message's content
            log.warning("live-view input failed (%s)", type(err).__name__)
            return "unsure"
        if op["t"] == "key" and op["k"] == "SelectAll":
            # It selects the address bar's text, which Chrome puts in the X
            # selection: that is not the operator's to copy (casa-test F32).
            session.quiet_selection_until = time.monotonic() + SELECTALL_QUIET_S
        return None

    # ------------------------------------------------------------------ probe, selection

    async def _session_loop(self, session: Session) -> None:
        last_ping = last_sel = 0.0
        last_sent = None
        cleared = False
        while self.sessions.get(session.run_id) is session and session.phase == "viewing" \
                and session.ctl:
            try:
                await asyncio.wait_for(session.poke.wait(), PROBE_S)
            except asyncio.TimeoutError:
                pass
            session.poke.clear()
            now = time.monotonic()
            message = sel = None
            async with self._screen:
                if self.sessions.get(session.run_id) is not session or session.phase != "viewing":
                    break
                bs = self.browser.session
                if bs is None:
                    continue
                if not cleared:
                    # A selection made before this view is never offered.
                    try:
                        await asyncio.wait_for(self.screen.clear_selection(), OP_TIMEOUT_S)
                        cleared = True
                    except Exception as err:  # noqa: BLE001 — retried next tick; no offer meanwhile
                        log.debug("selection not cleared (%s)", type(err).__name__)
                try:
                    probe = await asyncio.wait_for(self.screen.probe(bs), OP_TIMEOUT_S)
                    if probe is not None:
                        session.state = probe
                        message = viewer_state(probe, session.ack, session.refused, session.unsure)
                    if cleared and now - last_sel >= SELECTION_S:
                        last_sel = now
                        text = await asyncio.wait_for(self.screen.selection(), OP_TIMEOUT_S)
                        if text:
                            await asyncio.wait_for(self.screen.clear_selection(), OP_TIMEOUT_S)
                            # Looked at before and after the read: no offer from a page
                            # holding a typed password (a Tab into it selects its value).
                            if (len(text) <= MAX_SELECTION and selection_offerable(probe)
                                    and time.monotonic() >= session.quiet_selection_until):
                                after = await asyncio.wait_for(self.screen.probe(bs), OP_TIMEOUT_S)
                                if selection_offerable(after):
                                    sel = text
                except Exception as err:  # noqa: BLE001 — a navigation in flight, a closed page
                    log.debug("live-view probe skipped (%s)", type(err).__name__)
            if message is not None and (message != last_sent or session.resend):
                last_sent = message
                session.resend = False
                await self._send(session, message)
            if sel is not None:
                await self._send(session, {"t": "sel", "text": sel})
            if now - last_ping >= PING_S:
                last_ping = now
                await self._send(session, {"t": "ping"})


async def _close_ws(ws, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:  # noqa: BLE001
        pass
