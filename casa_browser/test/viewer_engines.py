"""The live view's viewer page (``static/viewer.html`` + ``live.js``) in real browser engines —
WebKit (as on an iPhone) and Chromium — with touch, through Playwright, against a stand-in
controller: the page and its scripts are served by request routing, the two WebSockets by
WebSocket routing and noVNC by a stand-in picture module (``FAKE_RFB``), so no app, display or
network is involved. The viewer runs inside a stand-in Home Assistant panel (``PANEL``): a
same-origin iframe, as Home Assistant's ingress serves it, whose top page can play the phone's
keyboard (its visual viewport shrinks and scrolls, as iOS does) and records what the viewer asks
of Home Assistant. Run by ``run-viewer-engines.sh`` in Playwright's own image (it carries both
engines). Exits nonzero on any failure. Synthetic only; nothing here is shipped.

What only a real iPhone can confirm is not claimed here: the real keyboard's geometry and
whether iOS shows it for a focus made in a pointerup handler, iOS's own scrolling of the top
page to a focused field, pinch gestures under real fingers (synthetic pointer events here), and
the Companion app's own kiosk handling.

Checks (live view K and the casa-test findings):
* K — no input over the picture, only the bar's hidden keyboard input (no keyboard help, F15);
  a tap is a click; on a field it opens a session bound to that field: keys wait for the tap's
  state, go live once Chrome's focus is confirmed there, never when it is elsewhere ("missed");
  a plain keyboard: each key event one key in Chrome by its inputType (text, Backspace,
  Ctrl+Backspace, Delete), anything else nothing and a line, the input's sentinel kept;
  composition once at its end; return closes the
  keyboard (no Enter), before the confirmation too; refused/unsure outcomes end the session with
  their line, also after Return; a closed socket, a dropped session and an unanswered batch are
  told; ⌨ never opens a session; paste: one read per gesture, once, bound to its session, not
  several lines; the debug trace never shows a password's characters;
* F34 — the keyboard closed, a tap off the fields, the field again: a new session;
* F35/F41 — the keyboard up: no refit of Chrome, the picture slides so the field is visible; User
  then Password each bound to its own field; the picture comes back after;
* F13 — Chrome's address bar: the keyboard comes up, the first keys replace the address, return
  goes;
* F14 — without a session the viewer fits the visible part of its frame and refits Chrome;
* F18/F33 — the toast in the bar; F37/F38 — every line fits, two lines at most, by measurement;
* F19 — one finger; − · Fit · + zoom the picture from the bar, off the picture (taps follow it); the page never scrolls; Done resets it;
* F24 — after a reconnect only the new picture is on screen;
* the picture shows and takes taps only once the first state named Chrome's toolbar, cropped;
* F29 — Home Assistant is asked to hide its header again at every viewport change, not after
  Done.
"""
from __future__ import annotations

import asyncio
import html
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

APP = Path(__file__).resolve().parents[1]
STATIC = APP / "rootfs/opt/casa_browser/static"
LOCALES = APP / "rootfs/opt/casa_browser/locales"
PREFIX, RUN = "/p", "r1"
BASE = "http://viewer.test"
W, H = 393, 659          # an iPhone 15's Safari viewport

# The top page: Home Assistant's panel in miniature. Its visual viewport is replaced by one the
# test moves (``__kbd(height, offsetTop)``), the frame can be shrunk as Home Assistant's header
# does (``__header(on)``), and every message the viewer posts is recorded.
PANEL = """<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{margin:0;height:100%;overflow:hidden}#h{height:40px;background:#03a9f4;display:none}
iframe{display:block;border:0;width:100%;height:100%;padding:0}</style>
<script>
const fake = new EventTarget(); fake._h = null; fake._top = 0;
for (const [k, f] of Object.entries({height: () => fake._h ?? innerHeight, width: () => innerWidth,
    offsetTop: () => fake._top, offsetLeft: () => 0, pageTop: () => fake._top, pageLeft: () => 0, scale: () => 1}))
  Object.defineProperty(fake, k, {get: f});
Object.defineProperty(window, 'visualViewport', {value: fake, configurable: true});
window.__kbd = (h, top) => { fake._h = h; fake._top = top || 0; fake.dispatchEvent(new Event('resize')); fake.dispatchEvent(new Event('scroll')); };
window.__msgs = [];
addEventListener('message', ev => {
  __msgs.push(ev.data);
  if (ev.data && ev.data.type === 'home-assistant/subscribe-properties')
    ev.source.postMessage({type: 'home-assistant/properties', narrow: true,
      safeAreaInsets: window.__insets || {top: '0px', right: '0px', bottom: '0px', left: '0px'}}, '*');
});
window.__header = on => { document.getElementById('h').style.display = on ? 'block' : 'none';
  document.querySelector('iframe').style.height = on ? 'calc(100% - 40px)' : '100%'; };
</script></head><body><div id="h"></div><iframe src="VIEWER"></iframe></body></html>"""

# noVNC in miniature: a 500×800 canvas in its own screen element; its disconnect takes noVNC's
# timeout on a dead socket (3 s) before the element goes.
FAKE_RFB = """let n = 0;
export default class RFB extends EventTarget {
  constructor(target, url, opts) {
    super(); this.id = ++n; this._target = target;
    this._screen = document.createElement('div'); this._screen.style.cssText = 'width:100%;height:100%';
    const c = document.createElement('canvas'); c.width = 500; c.height = 800; c.dataset.id = String(this.id);
    c.style.cssText = 'display:block;width:500px;height:800px';
    this._screen.appendChild(c); target.appendChild(this._screen);
    this._display = {_viewportLoc: {x: 0, y: 0, w: 500, h: 800},
      viewportChangePos(dx, dy) { this._viewportLoc.x += dx; this._viewportLoc.y += dy; }};
    this._clip = true;
    setTimeout(() => this.dispatchEvent(new Event('connect')), 30);
  }
  // as noVNC: a change of the clip puts the viewport back at (0, 0) (casa-test F36)
  get clipViewport() { return this._clip; }
  set clipViewport(v) { if (v !== this._clip) { this._display._viewportLoc.x = 0; this._display._viewportLoc.y = 0; } this._clip = v; }
  disconnect() {
    setTimeout(() => { try { this._target.removeChild(this._screen); } catch (e) {}
      this.dispatchEvent(new Event('disconnect')); }, 3000);
  }
}"""


def viewer_html() -> str:
    en = json.loads((LOCALES / "en.json").read_text())
    values = {"prefix": PREFIX, "run": RUN, "viewer": "v1", "nonce": "n1", "purpose": "assist",
              "done": f"{PREFIX}/view/{RUN}/done", "lang": "en",
              "strings": json.dumps({k: v for k, v in en.items() if k.startswith("js.")})}
    for key, value in en.items():
        if key.startswith(("viewer.", "js.")):
            values[key.replace(".", "_")] = value
    page = (STATIC / "viewer.html").read_text()
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", html.escape(value, quote=True))
    assert "{{" not in page, "a viewer placeholder is not filled"
    return page


def field(doc, ident, y, focused=False, kind="text", lb=None):
    return {"doc": doc, "fi": 0, "id": ident, "x": 20, "y": y, "w": 300, "h": 40, "type": kind,
            "focused": focused, "ac": "", "im": "", "lb": lb if lb is not None else f"F{ident}"}


def key(f):
    return {"fi": f["fi"], "doc": f["doc"], "id": f["id"]}


def state(fields, toolbar=0, doc=None, ack=0, refused=(), unsure=()):
    """Chrome's window at 0,0, 500×800; with ``toolbar`` its page starts that far down."""
    doc = doc if doc is not None else (fields[0]["doc"] if fields else 1)
    return {"t": "state", "win": [0, 0, 500, 800], "origin": [0, toolbar], "doc": doc, "ack": ack,
            "refused": list(refused), "unsure": list(unsure),
            "view": [500, 800 - toolbar], "host": "site.example", "title": "t", "fields": fields}


async def until(frame, expr, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if await frame.evaluate(expr):
            return True
        await asyncio.sleep(0.05)
    return False


POINTER = """([type, id, x, y]) => document.querySelector('#touch').dispatchEvent(new PointerEvent(type,
    {pointerId: id, clientX: x, clientY: y, bubbles: true, cancelable: true, pointerType: 'touch',
     isPrimary: id === 1}))"""
AT = """([X, Y]) => { const c = document.querySelector('#screen canvas'), r = c.getBoundingClientRect();
    return [r.left + X * r.width / c.width, r.top + Y * r.height / c.height]; }"""
ATTRS = """el => [el.getAttribute('autocapitalize'), el.getAttribute('autocorrect'), el.spellcheck,
    el.getAttribute('writingsuggestions'), el.getAttribute('autocomplete')]"""
NO_HELP = ["none", "off", False, "false", "off"]


async def scenario(pw, engine: str) -> list[str]:
    problems: list[str] = []
    try:
        await _scenario(pw, engine, problems)
    except Exception as err:  # noqa: BLE001 — a step that could not run is a failure too
        import traceback
        where = [f.lineno for f in traceback.extract_tb(err.__traceback__) if f.filename == __file__]
        problems.append(f"{engine}: stopped at line {where[-1] if where else '?'}: "
                        f"{type(err).__name__}: {str(err).splitlines()[0]}")
    return problems


async def _scenario(pw, engine: str, problems: list[str]) -> None:
    def bad(text):
        problems.append(f"{engine}: {text}")
    browser = await getattr(pw, engine).launch()
    context = await browser.new_context(viewport={"width": W, "height": H}, has_touch=True,
                                        is_mobile=True, device_scale_factor=3)
    page = await context.new_page()
    sent: list[dict] = []
    ctl: list = []

    async def serve(route):
        url = route.request.url
        if url.startswith(f"{BASE}{PREFIX}/view/{RUN}/state"):
            await route.fulfill(content_type="application/json", body='{"state": "viewing"}')
        elif url == f"{BASE}/panel":
            await route.fulfill(content_type="text/html",
                                body=PANEL.replace("VIEWER", f"{PREFIX}/view/{RUN}"))
        elif url == f"{BASE}{PREFIX}/view/{RUN}":
            await route.fulfill(content_type="text/html", body=viewer_html())
        elif url == f"{BASE}{PREFIX}/novnc/core/rfb.js":
            await route.fulfill(content_type="text/javascript", body=FAKE_RFB)
        elif url.startswith(f"{BASE}{PREFIX}/static/"):
            name = url.split("/static/", 1)[1]
            ctype = "text/javascript" if name.endswith(".js") else "text/css"
            await route.fulfill(content_type=ctype, body=(STATIC / name).read_text())
        else:
            await route.fulfill(status=404, body="")

    def on_ctl(ws):
        ctl.append(ws)
        ws.on_message(lambda m: sent.append(json.loads(m)))

    await page.route(f"{BASE}/**", serve)
    await page.route_web_socket(f"**{PREFIX}/view/{RUN}/ctl*", on_ctl)
    await page.route_web_socket(f"**{PREFIX}/view/{RUN}/websockify*", lambda ws: None)
    await page.goto(f"{BASE}/panel")
    for _ in range(100):
        if ctl and len(page.frames) > 1:
            break
        await asyncio.sleep(0.05)
    if not ctl:
        return bad("the control socket never opened")
    v = page.frames[1]
    if not await until(v, "() => document.querySelector('#screen canvas') !== null"):
        return bad("the picture never came")

    def maxq():
        return max([m["q"] for m in sent if isinstance(m.get("q"), int)] or [0])

    def confirm(fields, **kw):
        """A state probed after every message so far (it acknowledges the last ``q``); ``refused``
        lists sends the controller did not type."""
        ctl[-1].send(json.dumps(state(fields, ack=maxq(), **kw)))

    ws = ctl[-1]
    # the picture connected before the first state (as when the controller's first probe is
    # slow): hidden and deaf to taps until a state names Chrome's toolbar, so it never shows
    # uncropped and then jumps by the toolbar's height under a finger
    if not await until(v, "() => __lv.open()[1]"):
        return bad("the picture never connected")
    taps = len([m for m in sent if m.get("t") == "tap"])
    await page.touchscreen.tap(*(await v.evaluate(AT, [250, 400])))
    await asyncio.sleep(0.25)
    early = [await v.evaluate("getComputedStyle(document.querySelector('#screen')).visibility"),
             await v.evaluate("__lv.pan()"), len([m for m in sent if m.get("t") == "tap"]) - taps]
    if early != ["hidden", None, 0]:
        bad(f"before the first state the picture was [visibility, pan, taps sent] {early}")
    confirm([], toolbar=87)
    await asyncio.sleep(0.4)
    if (shown := [await v.evaluate("getComputedStyle(document.querySelector('#screen')).visibility"),
                  await v.evaluate("__lv.pan()")]) != ["visible", [87, 87]]:
        bad(f"after the first state the picture was [visibility, pan] {shown}")
    confirm([])
    await asyncio.sleep(0.4)

    async def kb():
        return await v.evaluate("__lv.kb()")

    async def tap_at(X, Y):
        await page.touchscreen.tap(*(await v.evaluate(AT, [X, Y])))
        await asyncio.sleep(0.25)

    async def note():
        return await v.evaluate("document.querySelector('#toast').hidden ? '' : document.querySelector('#toast').textContent")

    def acts(n):
        """(text or key name, field id, bar) of each text and key sent since n, in order;
        consecutive texts for one target joined (a typed word goes one message per key)."""
        out = []
        for m in sent[n:]:
            if m.get("t") not in ("text", "key"):
                continue
            item = [m["t"], m.get("s") if m["t"] == "text" else m.get("k"), (m.get("to") or {}).get("id"), m.get("bar")]
            if item[0] == "text" and out and out[-1][0] == "text" and out[-1][2:] == item[2:]:
                out[-1][1] += item[1]
            else:
                out.append(item)
        return [tuple(x) for x in out]

    def bound(n):
        """(text, field id, bar) of the texts sent since n, consecutive ones joined."""
        return [(s, i, b) for kind, s, i, b in acts(n) if kind == "text"]

    async def sentinel():
        return await v.evaluate("() => { const k = document.querySelector('#kb'); return [k.value, k.selectionStart, k.selectionEnd]; }")

    async def beforeinput(*events):
        """Synthetic beforeinput events [inputType, data] on the keyboard input; whether each
        was cancelled."""
        return await v.evaluate("""evs => evs.map(([it, data]) => {
            const e = new InputEvent('beforeinput', {inputType: it, data, cancelable: true, bubbles: true});
            document.querySelector('#kb').dispatchEvent(e); return e.defaultPrevented; })""", [list(e) for e in events])

    async def close_kb():
        await v.evaluate("document.querySelector('#kb').blur()")
        await asyncio.sleep(0.5)

    # K — nothing over the picture: the page's only text input is the hidden keyboard input in
    # the bar; it asks the phone's keyboard for no help (F15)
    inputs = await v.evaluate("[...document.querySelectorAll('input')].filter(i => i.type !== 'hidden')"
                              ".map(i => [i.id, i.closest('#pill') !== null])")
    if inputs != [["kb", True]]:
        bad(f"inputs on the page other than the bar's keyboard input: {inputs}")
    if (got := await v.eval_on_selector("#kb", ATTRS)) != NO_HELP:
        bad(f"the keyboard input asks for keyboard help: {got}")

    user, pw = field(2, 11, 100, lb="Username"), field(2, 12, 200, kind="password", lb="Password")
    confirm([user, pw])
    await asyncio.sleep(0.4)
    # a tap on a field: a click in Chrome, and the keyboard input takes the focus in the touch
    n = len(sent)
    await tap_at(170, 120)
    taps = [m for m in sent[n:] if m.get("t") == "tap"]
    k = await kb()
    if len(taps) != 1 or abs(taps[0]["x"] - 170) > 2 or abs(taps[0]["y"] - 120) > 2 or not k["focused"] \
            or k["sess"] != {"mode": "field", "ok": None, "key": key(user)}:
        bad(f"a tap on Username: taps {taps}, keyboard {k}")
    # an empty field: the strip shows Paste, not the arrows (they come with text)
    if not await v.evaluate("document.querySelector('#caretleft').hidden") or await v.evaluate("document.querySelector('#kbstrip').hidden"):
        bad("an empty field's strip shows the arrows, or no strip")
    # typed before the tap's state: held, then sent bound to Username once Chrome confirms it
    await page.keyboard.type("tom")
    await asyncio.sleep(0.2)
    ctl[-1].send(json.dumps(state([dict(user, focused=True), pw], ack=taps[0]["q"] - 1 if taps else 0)))
    await asyncio.sleep(0.3)
    if bound(n):
        bad(f"keys went before the tap was acknowledged: {bound(n)}")
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    if bound(n) != [("tom", 11, None)]:
        bad(f"after the confirmation: {bound(n)}")
    # live: each key as typed, one message each, bound and numbered; Backspace is a key
    n = len(sent)
    await page.keyboard.type("x")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.3)
    if acts(n) != [("text", "x", 11, None), ("key", "Backspace", 11, None)] \
            or not all(isinstance(m.get("q"), int) and "ok" in m for m in sent[n:]):
        bad(f"live keys: {sent[n:]}")
    if await sentinel() != ["\u200b\u200b", 1, 1]:
        bad(f"the keyboard input lost its sentinel: {await sentinel()}")
    # the plain keyboard: each inputType one action, mechanically; each event cancelled.
    # (an engine that does not know an inputType makes a synthetic one '': dictation is
    # checked where the engine has it — WebKit, as on the iPhone)
    known = await v.evaluate("it => new InputEvent('beforeinput', {inputType: it}).inputType === it", "insertFromDictation")
    n = len(sent)
    cancelled = await beforeinput(["insertText", "a"], ["deleteWordBackward", None], ["insertFromPaste", "p"],
                                  ["deleteContentForward", None], *([["insertFromDictation", "d"]] if known else []),
                                  ["deleteContentBackward", None])
    await asyncio.sleep(0.3)
    want = [("text", "a", 11, None), ("key", "CtrlBackspace", 11, None), ("text", "p", 11, None),
            ("key", "Delete", 11, None), *([("text", "d", 11, None)] if known else []), ("key", "Backspace", 11, None)]
    if acts(n) != want or len(sent) - n != len(want) or cancelled != [True] * len(want):
        bad(f"the inputType mapping: {acts(n)} ({len(sent) - n} messages, cancelled {cancelled})")
    if engine == "webkit" and not known:
        bad("WebKit does not know insertFromDictation: the dictation mapping went unchecked")
    # a paste whose text is in its data transfer, data null (where the engine takes one)
    n = len(sent)
    took = await v.evaluate("""() => { const dt = new DataTransfer(); dt.setData('text/plain', 'dt');
        const e = new InputEvent('beforeinput', {inputType: 'insertFromPaste', data: null, dataTransfer: dt, cancelable: true});
        document.querySelector('#kb').dispatchEvent(e); return !!e.dataTransfer; }""")
    await asyncio.sleep(0.2)
    if acts(n) != ([("text", "dt", 11, None)] if took else []):
        bad(f"a paste from its data transfer: {acts(n)} (engine took it: {took})")
    # text of several lines (a paste from the phone's own menu) is not typed: a line break would
    # be Return in Chrome
    n = len(sent)
    await v.evaluate("__lv.toast('')")
    await beforeinput(["insertFromPaste", "a\nb"])
    await asyncio.sleep(0.2)
    if sent[n:] or "line" not in (await note()):
        bad(f"a paste of several lines: sent {sent[n:]}, line {await note()!r}")
    # anything not mappable one to one: nothing sent, a line, the input unchanged
    for it in ("insertReplacementText", "deleteSoftLineBackward", "deleteByCut", "historyUndo", "formatBold"):
        n = len(sent)
        await v.evaluate("__lv.toast('')")
        cancelled = await beforeinput([it, "zz" if it.startswith("insert") else None])
        await asyncio.sleep(0.15)
        if sent[n:] or cancelled != [True] or "supported" not in (await note()) or (await kb())["sess"] is None:
            bad(f"an unmappable {it}: sent {sent[n:]}, cancelled {cancelled}, line {await note()!r}")
    if await sentinel() != ["\u200b\u200b", 1, 1]:
        bad(f"the sentinel after the unmappable events: {await sentinel()}")
    # an event that could not be cancelled still goes once; the sentinel comes back
    n = len(sent)
    await v.evaluate("""() => { const k = document.querySelector('#kb');
        k.dispatchEvent(new InputEvent('beforeinput', {inputType: 'insertText', data: 'u', cancelable: false}));
        k.value = '\u200bu\u200b'; k.dispatchEvent(new InputEvent('input', {inputType: 'insertText', data: 'u'})); }""")
    await asyncio.sleep(0.2)
    if acts(n) != [("text", "u", 11, None)] or await sentinel() != ["\u200b\u200b", 1, 1]:
        bad(f"an uncancellable key: {acts(n)} {await sentinel()}")
    # the caret moved off the middle (a drag on the space bar): it comes back, Backspace still fires
    await v.evaluate("document.querySelector('#kb').setSelectionRange(0, 0)")
    await asyncio.sleep(0.2)
    n = len(sent)
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    if acts(n) != [("key", "Backspace", 11, None)]:
        bad(f"Backspace after the caret moved: {acts(n)} {await sentinel()}")
    # composition: nothing while composing, once at its end
    n = len(sent)
    await v.evaluate("""() => { const k = document.querySelector('#kb');
        k.dispatchEvent(new CompositionEvent('compositionstart'));
        k.value = 'tomasé'; k.dispatchEvent(new InputEvent('input', {inputType: 'insertCompositionText', isComposing: true}));
        k.dispatchEvent(new CompositionEvent('compositionend', {data: 'é'})); }""")
    await asyncio.sleep(0.3)
    if bound(n) != [("é", 11, None)]:
        bad(f"composition: {bound(n)}")
    # a composition's own input types (WebKit commits with them) send nothing and say nothing:
    # its text went at compositionend
    n = len(sent)
    await v.evaluate("__lv.toast('')")
    kinds = await v.evaluate("its => its.filter(it => new InputEvent('beforeinput', {inputType: it}).inputType === it)",
                             ["insertFromComposition", "deleteCompositionText", "insertCompositionText"])
    if engine == "webkit" and len(kinds) != 3:
        bad(f"WebKit knows only {kinds} of the composition input types")
    cancelled = await beforeinput(*[[it, None if it.startswith("delete") else "é"] for it in kinds])
    await asyncio.sleep(0.2)
    if sent[n:] or "supported" in (await note()) or True in cancelled:
        bad(f"a composition's input types: sent {sent[n:]}, line {await note()!r}, cancelled {cancelled}")
    # return in a field closes the keyboard and sends no Enter (the page's button submits)
    n = len(sent)
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)
    if [m for m in sent[n:] if m.get("k") == "Enter"] or (await kb())["focused"] or (await kb())["sess"]:
        bad(f"return in a field: {sent[n:]} {await kb()}")

    # the binding: tap Password, type, the tap's state shows Username still focused (a dropdown
    # took the click) — nothing goes anywhere, never to Username, and a line says so
    n = len(sent)
    await tap_at(170, 220)
    if (await kb())["type"] != "password":
        bad(f"a password field's tap: {await kb()}")
    await page.keyboard.type("s3cret")
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.4)
    waiting = await kb()                  # Chrome may still be handling the click: it waits
    await asyncio.sleep(1.6)              # the tap's time runs out
    if waiting["sess"] is None or waiting["sess"]["ok"] is not None:
        bad(f"a state with the old focus ended or confirmed the session at once: {waiting}")
    if bound(n) or (await kb())["sess"] or "missed" not in (await note()):
        bad(f"a tap that did not reach Password still sent {bound(n)} / {await kb()} / {await note()!r}")
    # Chrome slow with the click: the first state still shows Username, the next one Password —
    # the session is confirmed on Password, keys held until then
    n = len(sent)
    await tap_at(170, 220)
    await page.keyboard.type("late")
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    confirm([user, dict(pw, focused=True)])
    await asyncio.sleep(0.3)
    if bound(n) != [("late", 12, None)]:
        bad(f"a late focus on the tapped field: {bound(n)}")
    await close_kb()
    # tap Password again, confirmed: the password goes bound to Password only
    n = len(sent)
    await tap_at(170, 220)
    await page.keyboard.type("pw!")
    confirm([user, dict(pw, focused=True)])
    await asyncio.sleep(0.4)
    if bound(n) != [("pw!", 12, None)]:
        bad(f"the password: {bound(n)}")
    # the page moves the focus to Username: the next keys are refused by the guard (the state
    # lists them) and the session ends with a line — nothing retargeted
    n = len(sent)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    if (await kb())["sess"] or "focus" not in (await note()) or bound(n):
        bad(f"the focus moved off Password: the session went on ({await kb()}, {await note()!r}, {bound(n)})")
    # refused and unsure outcomes end the session with their line
    for kind, words in (("refused", "focus"), ("unsure", "sure")):
        await tap_at(170, 220)
        confirm([user, dict(pw, focused=True)])
        await asyncio.sleep(0.3)
        n = len(sent)
        await page.keyboard.type("z")
        await asyncio.sleep(0.2)
        q = [m["q"] for m in sent[n:] if m.get("t") == "text"]
        ctl[-1].send(json.dumps(state([user, dict(pw, focused=True)], ack=maxq(), **{kind: q})))
        await asyncio.sleep(0.3)
        if (await kb())["sess"] or words not in (await note()):
            bad(f"a {kind} batch left the session open or said nothing: {await kb()} {await note()!r}")
    # an outcome that arrives after Return closed the session is still told (Astra K d2)
    await tap_at(170, 220)
    confirm([user, dict(pw, focused=True)])
    await asyncio.sleep(0.3)
    n = len(sent)
    await page.keyboard.type("y")
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.3)
    q = [m["q"] for m in sent[n:] if m.get("t") == "text"]
    ctl[-1].send(json.dumps(state([user, dict(pw, focused=True)], ack=maxq(), refused=q)))
    await asyncio.sleep(0.3)
    if "focus" not in (await note()):
        bad(f"a refusal after Return said nothing: {await note()!r}")
    # keys typed before the tap's state wait, then go bound and numbered, in order (Terra K d2)
    n = len(sent)
    await tap_at(170, 120)
    await page.keyboard.press("Backspace")
    await page.keyboard.type("Q")
    await asyncio.sleep(0.2)
    early = acts(n)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    if early or acts(n) != [("key", "Backspace", 11, None), ("text", "Q", 11, None)]:
        bad(f"keys before the confirmation: {early} then {acts(n)}")
    # many held keys: every one goes, once, in order (r2)
    await close_kb()
    n = len(sent)
    await tap_at(170, 120)
    await beforeinput(*([["deleteContentBackward", None]] * 300 + [["insertText", "!"]]))
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.4)
    if acts(n) != [("key", "Backspace", 11, None)] * 300 + [("text", "!", 11, None)]:
        bad(f"300 held backspaces went as {len(acts(n))} actions")
    # a composition under way when the tap is confirmed goes once, when it ends (r2)
    await close_kb()
    n = len(sent)
    await tap_at(170, 120)
    await v.evaluate("""() => { const k = document.querySelector('#kb');
        k.dispatchEvent(new CompositionEvent('compositionstart'));
        k.value = 'n'; k.dispatchEvent(new InputEvent('input', {inputType: 'insertCompositionText', isComposing: true})); }""")
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    await v.evaluate("""() => { const k = document.querySelector('#kb'); k.value = '你';
        k.dispatchEvent(new InputEvent('input', {inputType: 'insertCompositionText', isComposing: true}));
        k.dispatchEvent(new CompositionEvent('compositionend', {data: '你'})); }""")
    await asyncio.sleep(0.3)
    if bound(n) != [("你", 11, None)]:
        bad(f"a composition across the confirmation went as {bound(n)}")
    # a composition cut off by a tap on another field (no compositionend) does not block the
    # next session's keys (Astra K r3)
    await v.evaluate("""() => { const k = document.querySelector('#kb');
        k.dispatchEvent(new CompositionEvent('compositionstart'));
        k.value = '你n'; k.dispatchEvent(new InputEvent('input', {inputType: 'insertCompositionText', isComposing: true})); }""")
    await tap_at(170, 220)
    confirm([user, dict(pw, focused=True)])
    await asyncio.sleep(0.3)
    n = len(sent)
    await page.keyboard.type("abc")
    await asyncio.sleep(0.3)
    if "".join(t for t, i, _ in bound(n) if i == 12) != "abc":
        bad(f"after a cut-off composition the next field got {bound(n)}")
    # ...and when that composition does end later (the input's reset commits it), its text goes
    # nowhere: it belonged to the session it began in (Terra K r7)
    n = len(sent)
    await v.evaluate("""() => document.querySelector('#kb').dispatchEvent(new CompositionEvent('compositionend', {data: '你'}))""")
    await asyncio.sleep(0.3)
    if sent[n:]:
        bad(f"a composition of the previous session went to the next one: {sent[n:]}")
    await close_kb()
    await tap_at(170, 120)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    n = len(sent)
    await page.keyboard.press("Backspace")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    bks = [m for m in sent[n:] if m.get("t") == "key"]
    if [(m["k"], (m.get("to") or {}).get("id"), isinstance(m.get("q"), int)) for m in bks] != \
            [("Backspace", 11, True), ("Backspace", 11, True)]:
        bad(f"backspaces went as {bks}")
    else:
        ctl[-1].send(json.dumps(state([dict(user, focused=True), pw], ack=maxq(), refused=[bks[-1]["q"]])))
        await asyncio.sleep(0.3)
        if (await kb())["sess"] or "focus" not in (await note()):
            bad("a refused Backspace left the session open or said nothing")
    # return before the tap's state: the keyboard goes at once, the text goes once confirmed
    n = len(sent)
    await tap_at(170, 120)
    await page.keyboard.type("ab")
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.4)
    early = await kb()
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    if early["focused"] or bound(n) != [("ab", 11, None)] or (await kb())["sess"]:
        bad(f"return before the confirmation: {early} {bound(n)} {await kb()}")
    # a session ended with text that never went out says so (here: no field confirmed)
    await tap_at(170, 220)
    await page.keyboard.type("lost")
    confirm([user, pw])                                           # nothing editable focused
    await asyncio.sleep(1.9)
    if "missed" not in (await note()):
        bad(f"text dropped without a line: {await note()!r}")
    # a tap whose state shows no field at all, nothing typed: still told at the deadline (r4)
    await tap_at(170, 220)
    confirm([user, pw])
    await asyncio.sleep(1.9)
    if (await kb())["sess"] or "missed" not in (await note()):
        bad(f"a silent miss: {await kb()} {await note()!r}")
    # a closed control socket: nothing is sent, the session ends with a line on the new socket
    await tap_at(170, 120)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    sockets, n = len(ctl), len(sent)
    await ctl[-1].close()
    await until(v, "() => !__lv.open()[0]", 3)
    await page.keyboard.type("offline")
    for _ in range(100):
        if len(ctl) > sockets:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.6)
    if bound(n) or (await kb())["sess"] or "Not sent" not in (await note()):
        bad(f"typing on a closed socket: {bound(n)} {await kb()} {await note()!r}")
    confirm([user, pw])
    await asyncio.sleep(0.3)

    # a batch sent but never answered when the socket goes: "not sure" on the new socket
    await tap_at(170, 120)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    await page.keyboard.type("u")
    await asyncio.sleep(0.2)
    sockets = len(ctl)
    await ctl[-1].close()
    for _ in range(100):
        if len(ctl) > sockets:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.6)
    if (await kb())["sess"] or "sure" not in (await note()):
        bad(f"an unanswered batch across a reconnect said nothing: {await kb()} {await note()!r}")
    confirm([user, pw])
    await asyncio.sleep(0.3)

    # paste (F46): one read at a time, inserted once, to the session that asked, with a line;
    # a read that ends after the field changed goes nowhere
    await v.evaluate("""() => { window.__reads = 0; window.__resolve = [];
        Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
            readText: () => new Promise(r => { window.__reads++; window.__resolve.push(r); })}}); }""")
    await tap_at(170, 120)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    n = len(sent)
    if await v.evaluate("document.querySelector('#pastebtn').hidden"):
        bad(f"no Paste pill while typing: {await kb()} {await v.evaluate('[!!navigator.clipboard, document.activeElement.id]')}")
    await v.tap("#pastebtn")
    await asyncio.sleep(0.3)
    await v.dispatch_event("#pastebtn", "pointerup")      # a second tap while iOS's callout is up
    await v.dispatch_event("#pastebtn", "click")          # and a late click
    await asyncio.sleep(0.3)
    await v.evaluate("() => window.__resolve.forEach(r => r('code42'))")
    await asyncio.sleep(0.4)
    reads = await v.evaluate("window.__reads")
    if reads != 1 or bound(n) != [("code42", 11, None)] or "Pasted" not in (await note()):
        bad(f"paste: {reads} reads, sent {bound(n)}, line {await note()!r}")
    if await v.evaluate("document.querySelector('#caretleft').hidden"):
        bad("after a paste the field holds text, but the arrows are hidden")
    # iOS's Allow Paste prompt takes the focus (and may send the page to the
    # background): the pending paste keeps its session, and its text goes to that field, bound,
    # on the new socket
    confirm([dict(user, focused=True), pw])                 # the earlier paste answered
    await asyncio.sleep(0.3)
    n = len(sent)
    await v.dispatch_event("#pastebtn", "pointerup")
    await asyncio.sleep(0.2)
    await v.evaluate("document.querySelector('#kb').blur()")
    await asyncio.sleep(0.5)
    sockets = len(ctl)
    await ctl[-1].close()
    for _ in range(100):
        if len(ctl) > sockets:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.6)
    alive = (await kb())["sess"]
    await v.evaluate("() => window.__resolve.slice(-1)[0]('allowed1')")
    await asyncio.sleep(0.6)
    if not alive or bound(n) != [("allowed1", 11, None)]:
        bad(f"a paste across iOS prompt: session {alive}, sent {bound(n)}, line {await note()!r} {(await v.evaluate('__lv.trace()'))[-14:]}")
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    n = len(sent)
    await v.dispatch_event("#pastebtn", "pointerup")
    await asyncio.sleep(0.2)
    await tap_at(170, 220)                                         # another field meanwhile,
    confirm([user, dict(pw, focused=True)])                        # confirmed
    await asyncio.sleep(0.3)
    await v.evaluate("() => window.__resolve.slice(-1)[0]('pw-in-clipboard')")
    await asyncio.sleep(0.4)
    if any(t == "pw-in-clipboard" for t, *_ in bound(n)):
        bad(f"a paste read for Username went to the next field: {bound(n)}")
    # one tap with a read that settles at once: one read, one insertion (pointerup and its click
    # are one gesture — Astra K d2); a text of several lines is not pasted, and says so
    await tap_at(170, 120)
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    await v.evaluate("""() => { window.__reads = 0; Object.defineProperty(navigator, 'clipboard', {configurable: true,
        value: {readText: async () => { window.__reads++; return window.__clip; }}}); window.__clip = 'quick'; }""")
    n = len(sent)
    await v.tap("#pastebtn")
    await asyncio.sleep(0.5)
    if await v.evaluate("window.__reads") != 1 or bound(n) != [("quick", 11, None)]:
        bad(f"one tap with a quick read: {await v.evaluate('window.__reads')} reads, sent {bound(n)}")
    await v.evaluate("window.__clip = 'line1\\nline2'")
    n = len(sent)
    await asyncio.sleep(1.6)
    await v.tap("#pastebtn")
    await asyncio.sleep(0.5)
    if bound(n) or "line" not in (await note()):
        bad(f"a multi-line paste: sent {bound(n)}, line {await note()!r}")
    # iOS's prompt denied (the read rejects): nothing goes, and a line says so (Terra t14s-d1)
    await v.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {configurable: true,
        value: {readText: async () => { window.__reads++; throw new DOMException('denied', 'NotAllowedError'); }}})""")
    n = len(sent)
    await asyncio.sleep(1.6)
    await v.tap("#pastebtn")
    await asyncio.sleep(0.5)
    if bound(n) or "clipboard" not in (await note()):
        bad(f"a denied paste: sent {bound(n)}, line {await note()!r}")
    await close_kb()
    confirm([user, pw])
    await asyncio.sleep(0.3)

    # the debug trace never shows a password's characters (an astral one included, Astra K r1)
    await tap_at(170, 220)
    await v.evaluate("""() => document.querySelector('#kb').dispatchEvent(new KeyboardEvent('keydown', {key: '\\u{1D49C}'}))""")
    await close_kb()
    trace = "\n".join(await v.evaluate("__lv.trace()"))
    if "\U0001D49C" in trace:
        bad("the debug trace shows an astral password character")
    if "s3cret" in trace or "keydown s\n" in trace + "\n" or "pw!" in trace:
        bad("the debug trace shows a password's characters")

    # F34 — the keyboard closed, a tap off the fields, then the field again: a new session,
    # confirmed, and its keys go to the field
    n = len(sent)
    await tap_at(170, 400)
    if (await kb())["sess"]:
        bad("a tap off the fields raised the keyboard")
    await tap_at(170, 120)
    await page.keyboard.type("+34")
    confirm([dict(user, focused=True), pw])
    await asyncio.sleep(0.3)
    if [t for t in sent[n:] if t.get("t") == "tap"].__len__() != 2 or bound(n) != [("+34", 11, None)]:
        bad(f"F34: {[(m['t'], m.get('x'), m.get('y')) for m in sent[n:] if m['t'] in ('tap', 'text')]}")
    await close_kb()

    # F41/F35 — keyboard up: no refit of Chrome while typing; the picture slides so the field is
    # in the visible part; User then Password low on the page, each batch bound to its field;
    # after the keyboard the picture comes back
    low_user, low_pw = field(3, 21, 560, lb="User"), field(3, 22, 620, kind="password", lb="Pass")
    confirm([low_user, low_pw])
    await asyncio.sleep(0.4)
    n = len(sent)
    await tap_at(170, 580)
    await page.evaluate("__kbd(323, 0)")
    await asyncio.sleep(0.6)
    confirm([dict(low_user, focused=True), low_pw])
    await asyncio.sleep(0.4)
    y_user = (await v.evaluate("__lv.at(170, 580)"))[1]
    await page.keyboard.type("tom")
    await asyncio.sleep(0.2)
    await tap_at(170, 640)
    confirm([low_user, dict(low_pw, focused=True)])
    await asyncio.sleep(0.4)
    y_pw = (await v.evaluate("__lv.at(170, 640)"))[1]
    await page.keyboard.type("pw")
    await asyncio.sleep(0.3)
    fits = [m for m in sent[n:] if m.get("t") == "fit"]
    if fits or not (36 < y_user < 323) or not (36 < y_pw < 323):
        bad(f"F41: fits {fits}, User at {y_user:.0f}, Pass at {y_pw:.0f} (visible 36–323)")
    per_field = {}
    for text_, ident, _ in bound(n):
        per_field[ident] = per_field.get(ident, "") + text_
    if per_field != {21: "tom", 22: "pw"}:
        bad(f"F35: {bound(n)}")
    # the strip (Paste · ← · →) sits at the bottom of the visible part, the field above it
    strip = await v.evaluate("""(() => { const s = document.querySelector('#kbstrip'), r = s.getBoundingClientRect();
        return [s.hidden, Math.round(r.top), Math.round(r.bottom)]; })()""")
    y_pw = (await v.evaluate("__lv.at(170, 640)"))[1]
    if strip[0] or not (323 - 47 - 1 <= strip[1] <= 323 - 47 + 1) or not (strip[2] <= 323) or not (36 < y_pw < strip[1]):
        bad(f"the strip: {strip}, Pass at {y_pw:.0f} (visible 36–323)")
    # ← / → send exactly one key each, bound to the field, and the keyboard stays
    n = len(sent)
    await v.tap("#caretleft")
    await v.tap("#caretleft")
    await v.tap("#caretright")
    await asyncio.sleep(0.3)
    if acts(n) != [("key", "Left", 22, None), ("key", "Left", 22, None), ("key", "Right", 22, None)] \
            or not (await kb())["focused"] or not (await kb())["sess"]:
        bad(f"the strip's arrows: {acts(n)} {await kb()}")
    # a probe older than the last key sent says nothing about the field now: the arrows stay
    ctl[-1].send(json.dumps(state([low_user, dict(low_pw, focused=True, tx=False)], ack=maxq() - 1)))
    await asyncio.sleep(0.3)
    if await v.evaluate("document.querySelector('#caretleft').hidden"):
        bad("a stale probe (not acknowledging the last key) hid the arrows")
    # a fresh probe (acknowledging every key) that finds the field empty hides the arrows
    ctl[-1].send(json.dumps(state([low_user, dict(low_pw, focused=True, tx=False)], ack=maxq())))
    await asyncio.sleep(0.3)
    if not await v.evaluate("document.querySelector('#caretleft').hidden"):
        bad("an empty field (fresh probe) still shows the arrows")
    # a field at the very bottom of the picture: it comes into sight above the strip, and the
    # picture's bottom stays at or below the visible bottom (no black band above the keyboard)
    low_end = field(3, 23, 760, lb="End")
    await close_kb()
    await page.evaluate("__kbd(null, 0)")
    await asyncio.sleep(0.6)
    confirm([low_user, low_pw, low_end])
    await asyncio.sleep(0.3)
    await tap_at(170, 775)
    await page.evaluate("__kbd(323, 0)")
    await asyncio.sleep(0.4)
    confirm([low_user, low_pw, dict(low_end, focused=True)])
    await asyncio.sleep(0.5)
    y_end = (await v.evaluate("__lv.at(170, 775)"))[1]
    pic_bottom = await v.evaluate("document.querySelector('#screen canvas').getBoundingClientRect().bottom")
    strip_top = await v.evaluate("document.querySelector('#kbstrip').getBoundingClientRect().top")
    if not (36 < y_end < strip_top) or pic_bottom < strip_top - 1:
        bad(f"a field at the picture's bottom: at {y_end:.0f}, picture bottom {pic_bottom:.0f}, strip at {strip_top:.0f} (visible 36–323) {await kb()} {(await v.evaluate('__lv.trace()'))[-8:]}")
    await close_kb()
    await page.evaluate("__kbd(null, 0)")
    await asyncio.sleep(0.8)
    if (await v.evaluate("__lv.zoom()"))["y"] != 0 or abs((await v.evaluate("__lv.at(170, 580)"))[1]
                                                        - (await v.evaluate(AT, [170, 580]))[1]) > 1:
        bad("after the keyboard the picture did not come back")

    # F14 — without a session the viewer fits the visible part of the frame (Home Assistant's
    # header, a rotation) and refits Chrome
    fits0 = len([m for m in sent if m.get("t") == "fit"])
    await page.evaluate("__kbd(500, 100)")
    await asyncio.sleep(0.6)
    box = await v.evaluate("() => { const r = document.querySelector('#app').getBoundingClientRect(); return [r.top, r.height]; }")
    if box != [100, 500] or len([m for m in sent if m.get("t") == "fit"]) <= fits0:
        bad(f"without a session the viewer did not fit the visible part: {box}")
    await page.evaluate("__kbd(null, 0)")
    await asyncio.sleep(0.6)

    # F18 — the toast: in the bar, clear of the controls, inside the safe areas; F33 centred
    await page.evaluate("window.__insets = {top: '47px', right: '0px', bottom: '34px', left: '0px'}")
    await v.evaluate("dispatchEvent(new Event('resize'))")     # a viewport change asks again
    await asyncio.sleep(0.5)
    ctl[-1].send(json.dumps({"t": "sel", "text": "blank"}))
    await asyncio.sleep(0.4)
    toast = await v.evaluate("""() => { const r = s => document.querySelector(s).getBoundingClientRect();
        const t = r('#toast'), p = r('#pill'), k = r('#done'), s = r('#screen');
        return {hidden: document.querySelector('#toast').hidden, t: [t.left, t.top, t.right, t.bottom],
                pill: [p.top, p.bottom], kb: k.left, screen: s.top,
                host: [r('#host').width, r('#host').right],
                lh: getComputedStyle(document.querySelector('#toast')).lineHeight}; }""")
    t = toast["t"]
    if (toast["hidden"] or t[0] < 0 or t[1] < 47 or t[3] > toast["pill"][1] or t[2] > toast["kb"]
            or t[3] > toast["screen"] or toast["host"][0] < 30 or toast["host"][1] > t[0]):
        bad(f"the copy toast is not in its place in the bar: {toast}")
    if toast["lh"] != "30px":
        bad(f"the toast's line height is {toast['lh']}, not its pill's 30px")
    await page.evaluate("window.__insets = {top: '0px', right: '0px', bottom: '0px', left: '0px'}")
    await v.evaluate("dispatchEvent(new Event('resize'))")
    await asyncio.sleep(0.5)

    # F37/F38 — every line the viewer says fits the bar at a phone's width, in every language:
    # two lines at most, chosen by measurement
    for lang in ("en", "it", "nl", "de"):
        table = json.loads((LOCALES / f"{lang}.json").read_text())
        for k in ("js.unsent", "js.unsure", "js.retap", "js.dropped", "js.not_pasted", "js.paste_refused",
                  "js.unsupported", "js.multiline", "js.pasted"):
            fits = await v.evaluate("""m => { __lv.toast(m); const t = document.querySelector('#toast');
                return [t.scrollWidth <= t.clientWidth + 1, t.scrollHeight <= t.clientHeight + 1]; }""", table[k])
            if fits != [True, True]:
                bad(f"F38: the {lang} line {table[k]!r} does not fit the bar: {fits}")

    # F13 — Chrome's toolbar shown, a tap on its address bar raises the keyboard; the first keys
    # replace the address; return goes (Enter)
    confirm([], toolbar=87)
    await asyncio.sleep(0.4)
    await v.tap("#host")
    await asyncio.sleep(0.6)
    await tap_at(250, 400)                                # the page: no keyboard (control)
    if (await kb())["sess"]:
        bad("a tap on the page raised the keyboard")
    n = len(sent)
    await tap_at(250, 64)
    if (await kb())["sess"] != {"mode": "bar", "ok": True, "key": None}:
        bad(f"a tap on the address bar did not raise the keyboard for it: {await kb()}")
    await page.keyboard.type("site.example/x")
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.4)
    typed = "".join(m["s"] for m in sent[n:] if m.get("t") == "text" and m.get("bar"))
    keys_ = [m["k"] for m in sent[n:] if m.get("t") == "key"]
    if typed != "site.example/x" or keys_ != ["SelectAll", "Enter"]:
        bad(f"the address went as {typed!r} with keys {keys_}")
    # in the address bar keys are keys too: only the session's first one is preceded by SelectAll
    await tap_at(250, 64)
    n = len(sent)
    await page.keyboard.type("ab")
    await page.keyboard.press("Backspace")
    await page.keyboard.type("c")
    await asyncio.sleep(0.3)
    got = [(m["t"], m.get("k") or m.get("s"), m.get("bar")) for m in sent[n:] if m.get("t") in ("text", "key")]
    if got != [("key", "SelectAll", True), ("text", "a", True), ("text", "b", True),
               ("key", "Backspace", True), ("text", "c", True)]:
        bad(f"keys in the address bar went as {got}")
    await close_kb()
    await v.tap("#host")
    confirm([])
    await asyncio.sleep(0.6)

    # F19 (operator ruling 2026-10-26): one finger only; the picture's zoom is the
    # − · Fit · + controls; zoomed, a swipe pans the picture, then scrolls Chrome at the edge
    confirm([field(5, 41, 300)])
    await asyncio.sleep(0.5)
    if (got := await v.evaluate("getComputedStyle(document.querySelector('#touch')).touchAction")) != "none":
        bad(f"the picture lets the browser zoom or scroll the page (touch-action {got})")
    width0 = await v.evaluate("document.querySelector('#screen canvas').getBoundingClientRect().width")
    n = len(sent)
    for args in (["pointerdown", 1, 100, 300], ["pointerdown", 2, 200, 300],
                 ["pointermove", 2, 250, 300], ["pointermove", 2, 300, 300], ["pointermove", 1, 100, 200],
                 ["pointerup", 2, 300, 300], ["pointerup", 1, 100, 200]):
        await v.evaluate(POINTER, args)
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)
    two = await v.evaluate("""() => { const e = new Event('touchstart', {bubbles: true, cancelable: true});
        Object.defineProperty(e, 'touches', {value: [{}, {}]}); document.querySelector('#touch').dispatchEvent(e);
        return e.defaultPrevented; }""")
    if await v.evaluate("__lv.zoom()") != {"z": 1, "x": 0, "y": 0} or [m for m in sent[n:] if m.get("t") != "fit"] or not two:
        bad(f"a two-finger gesture changed something: {await v.evaluate('__lv.zoom()')}, sent {sent[n:]}, touchstart held {two}")
    if not await v.evaluate("document.querySelector('#zoomfit').hidden"):
        bad("Fit shows while the picture is not zoomed")
    # a second finger during a drag: the drag ends at its last point (one up), then nothing more
    # until every finger lifted (Astra t14s-d1)
    n = len(sent)
    await v.evaluate(POINTER, ["pointerdown", 1, 100, 300])
    await asyncio.sleep(0.75)                                 # held: a mouse drag starts
    await v.evaluate(POINTER, ["pointermove", 1, 110, 320])
    for args in (["pointerdown", 2, 200, 300], ["pointermove", 1, 140, 360], ["pointermove", 2, 260, 300],
                 ["pointerup", 2, 260, 300], ["pointermove", 1, 150, 380], ["pointerup", 1, 150, 380]):
        await v.evaluate(POINTER, args)
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    drag = [m for m in sent[n:] if m.get("t") != "fit"]
    if [m.get("t") for m in drag] != ["down", "move", "up"] or (drag[2]["x"], drag[2]["y"]) != (drag[1]["x"], drag[1]["y"]):
        bad(f"a second finger during a drag: {drag}")
    # a finger still down on the picture when + is pressed taps nothing (the picture moved under it)
    n = len(sent)
    await v.evaluate(POINTER, ["pointerdown", 1, 120, 300])
    await v.tap("#zoomin")
    await v.evaluate(POINTER, ["pointerup", 1, 120, 300])
    await asyncio.sleep(0.3)
    if [m for m in sent[n:] if m.get("t") != "fit"] or abs((await v.evaluate("__lv.zoom()"))["z"] - 1.5) > 0.01:
        bad(f"a tap pending over +: sent {sent[n:]}, zoom {await v.evaluate('__lv.zoom()')}")
    await v.tap("#zoomfit")
    await asyncio.sleep(0.2)
    await v.tap("#zoomin")
    await v.tap("#zoomin")
    await asyncio.sleep(0.4)
    z = await v.evaluate("__lv.zoom()")
    width1 = await v.evaluate("document.querySelector('#screen canvas').getBoundingClientRect().width")
    if abs(z["z"] - 2) > 0.05 or abs(width1 / width0 - 2) > 0.1 or await v.evaluate("document.querySelector('#zoomfit').hidden"):
        bad(f"+ twice did not zoom the picture 2× with Fit shown: {z}, picture {width0} → {width1}")
    # Operator 2026-11-06, on an iPhone: the control sat exactly over a site's button. All three
    # are in the bar, in view, off the picture; Done and the site's name keep their place.
    place = await v.evaluate("""() => { const r = (s) => document.querySelector(s).getBoundingClientRect();
        const z = ['#zoomout', '#zoomfit', '#zoomin'].map(r), t = r('#touch'), d = r('#done'), h = r('#host');
        return {off: z.every(b => b.bottom <= t.top && b.width > 0 && b.right <= d.left && b.left >= h.right),
                done: d.right <= innerWidth && d.width > 0, host: h.width, width: innerWidth}; }""")
    if not place["off"] or not place["done"] or place["host"] < 60 or place["width"] > 500:
        bad(f"the zoom controls are not in the bar, off the picture, on a phone: {place}")
    scrolled = await page.evaluate("[scrollX, scrollY, document.querySelector('iframe').contentWindow.scrollY]")
    if scrolled != [0, 0, 0]:
        bad(f"the page scrolled: {scrolled}")
    n = len(sent)
    target = await v.evaluate(AT, [120, 330])
    await v.evaluate(POINTER, ["pointerdown", 3, *target])
    await v.evaluate(POINTER, ["pointerup", 3, *target])
    await asyncio.sleep(0.3)
    taps = [m for m in sent[n:] if m.get("t") == "tap"]
    if not taps or abs(taps[0]["x"] - 120) > 2 or abs(taps[0]["y"] - 330) > 2 or \
            ((await v.evaluate("__lv.kb()"))["sess"] or {}).get("key") != key(field(5, 41, 300)):
        bad(f"a tap on the zoomed field reached Chrome as {taps}, not a click at (120, 330) with its session")
    await v.evaluate("document.querySelector('#kb').blur()")
    await asyncio.sleep(0.4)
    # zoomed, a swipe pans the picture first and sends no scroll while it can pan
    z0 = await v.evaluate("__lv.zoom()")
    n = len(sent)
    # (240 px: more than a wheel notch's worth at 2×, so a swipe that also scrolled would show it)
    for args in (["pointerdown", 5, 200, 400], ["pointermove", 5, 200, 380], ["pointermove", 5, 200, 340],
                 ["pointermove", 5, 200, 280], ["pointermove", 5, 200, 220], ["pointermove", 5, 200, 160],
                 ["pointerup", 5, 200, 160]):
        await v.evaluate(POINTER, args)
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.3)
    z1 = await v.evaluate("__lv.zoom()")
    if abs((z1["y"] - z0["y"]) + 240) > 1 or [m for m in sent[n:] if m.get("t") == "scroll"]:
        bad(f"a zoomed swipe did not pan the picture: {z0} → {z1}, sent {sent[n:]}")
    # ...and at the picture's edge the same swipe carries on scrolling Chrome
    n = len(sent)
    await v.evaluate(POINTER, ["pointerdown", 6, 200, 600])
    for y in range(580, -1200, -40):
        await v.evaluate(POINTER, ["pointermove", 6, 200, y])
    await v.evaluate(POINTER, ["pointerup", 6, 200, -1200])
    await asyncio.sleep(0.3)
    z2 = await v.evaluate("__lv.zoom()")
    notches = sum(m["n"] for m in sent[n:] if m.get("t") == "scroll")
    if notches <= 0 or abs(z2["y"] - (await v.evaluate("-(__lv.zoom().z - 1) * (document.querySelector('#screen').getBoundingClientRect().height / __lv.zoom().z)"))) > 2:
        bad(f"at the picture's edge the swipe did not scroll Chrome: zoom {z2}, notches {notches}")
    # Fit: back to scale 1, from any zoom
    for _ in range(2):                    # to 4×, the most
        await v.tap("#zoomin")
    if not await v.evaluate("document.querySelector('#zoomin').disabled"):
        bad(f"+ is not disabled at the most zoom: {await v.evaluate('__lv.zoom()')}")
    await v.tap("#zoomfit")
    await asyncio.sleep(0.3)
    if await v.evaluate("__lv.zoom()") != {"z": 1, "x": 0, "y": 0} or not await v.evaluate("document.querySelector('#zoomfit').hidden"):
        bad(f"Fit did not restore the picture: {await v.evaluate('__lv.zoom()')}")

    # F24 — a reconnect while the old picture is still disconnecting: only the new one shows
    first = await v.evaluate("document.querySelector('#screen canvas').dataset.id")
    await v.evaluate("""() => {
        Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
        Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
        document.dispatchEvent(new Event('visibilitychange')); }""")
    await until(v, f"() => [...document.querySelectorAll('#screen canvas')].some(c => c.dataset.id !== '{first}')")
    await asyncio.sleep(0.3)
    canvases = await v.evaluate("[...document.querySelectorAll('#screen canvas')].map(c => c.dataset.id)")
    if len(canvases) != 1 or canvases[0] == first:
        bad(f"after a reconnect the screen holds pictures {canvases} (the old one was {first})")

    # F29 — Home Assistant is asked again at every viewport change, and no more after Done
    async def asks():
        return await page.evaluate(
            "__msgs.filter(m => m && m.type === 'home-assistant/subscribe-properties').length")
    before = await asks()
    await page.evaluate("__header(true)")                  # the header came back: the frame shrank
    await asyncio.sleep(0.5)
    if await asks() <= before:
        bad("the header came back and the viewer did not ask to hide it again")
    await page.evaluate("__header(false)")
    await asyncio.sleep(0.3)
    # Done: the zoom ends with the view, the header is given back, nothing more is asked
    await v.evaluate("""() => document.querySelector('#doneform').addEventListener('submit', ev => {
        window.__zoomAtDone = __lv.zoom(); ev.preventDefault(); })""")
    await v.tap("#zoomin")                # zoomed right before Done: Done's own reset is what is checked
    await asyncio.sleep(0.2)
    if (await v.evaluate("__lv.zoom()"))["z"] <= 1.01:
        bad(f"+ before Done did not zoom: {await v.evaluate('__lv.zoom()')}")
    await v.tap("#done")
    await asyncio.sleep(0.4)
    if (got := await v.evaluate("window.__zoomAtDone")) != {"z": 1, "x": 0, "y": 0}:
        bad(f"Done left the picture zoomed: {got}")
    if not await page.evaluate("__msgs.some(m => m && m.type === 'home-assistant/unsubscribe-properties')"):
        bad("Done did not give Home Assistant its header back")
    after_done = await asks()
    await page.evaluate("__header(true)")
    await asyncio.sleep(0.5)
    if await asks() != after_done:
        bad("after Done the viewer still asked Home Assistant to hide its header")
    await browser.close()


async def main() -> int:
    async with async_playwright() as pw:
        problems = []
        for engine in ("webkit", "chromium"):
            found = await scenario(pw, engine)
            print(f"{engine}: {'ok' if not found else 'FAILED'}", flush=True)
            problems += found
    for p in problems:
        print("  " + p)
    print(f"viewer engines: {2 - len({p.split(':')[0] for p in problems})} of 2 engines passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
