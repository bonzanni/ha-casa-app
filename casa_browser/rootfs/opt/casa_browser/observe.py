"""Observations and operator screenshots, behind the page-view gate (Plan 1 Task 7).

``Observer.observe`` returns what a model may see of the current page: its visible text and
its visible, actionable elements as opaque handles with masked labels — never an address, a
selector or an attribute. An ordinary field also shows what it holds (slice 5j, operator ruling
2026-10-31: its value masked and bounded, a select's chosen option, a checkbox's or radio's
state) under the screenshots' one rule, as the field is when it is read: a password,
one-time-code or payment-card field (``gate.SECRET_JS``'s ``hidesSecret``) shows none, and every
field whose value is shown is asked that rule again right after the reading. A page that later
changes its own field (its type, its autocomplete, its place) shows what it then holds: the
residual of ruling 2026-10-20, a page can show its own value as text.
``Observer.capture_operator`` (the operator's own
screenshot) and ``Observer.capture_question`` (a question's picture, its target outlined) store
a screenshot for the operator only, never for a model, under one rule (``Observer._capture``,
slice 5h): password, one-time-code and payment-card fields and every child frame are blacked
out; every other field shows what it holds, and text the page itself renders stays visible,
secret-looking or not (operator rulings 2026-10-20 and 2026-10-29: simple redaction of
recognised secret fields). Both:

* refuse while a live view or a broker login holds the browser (``ObservationRefused``);
* run the page-view gate on the page's full current address — fragment and client-side route
  included — before reading anything: ``page_view_allowed`` asks the run policy whether that
  address belongs to the session as a top-level document (in an account session, the site's
  origins and login origins; in the public session, any address the floor admits); a lease, an
  infrastructure host or a non-web scheme never qualifies;
* read the page with fixed extraction code in the isolated world (no model-supplied script);
* check the page generation and address again afterwards, and discard the result if the page
  moved meanwhile.

Frames: a frame whose address the gate does not admit is left out of an observation (its
place is noted); in a screenshot every child frame is blacked out whole, and the image is cropped to the page's content (``crop_png``).
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from casa_browser.gate import CONTROLS_JS, FIELD_KINDS, INPUT_KINDS, MULTI_LINE, SECRET_JS
from casa_browser.handles import HandleRegistry, PrivateTarget
from casa_browser.masking import MASK, mask_text
from casa_browser.policy import RequestMeta, RunPolicy, decide
from casa_browser.protocol import ElementView, Observation

VIEW_REASONS = frozenset({"site_document", "public_session"})
MAX_ELEMENTS = 400
MAX_BLOCKS = 1500
MAX_VALUE = 500          # characters of a field's value read (slice 5j); a longer one ends in " …"
MAX_VALUE_SHOWN = 600    # after masking, which may lengthen it
FRAME_WITHHELD = "[a frame on this page is not shown]"
MASK_ATTR = "data-casa-mask"
# The question's target outline: set on the element's own inline style for the capture and put
# back after (slice 5f: a stylesheet node added for it would change selectors such as
# :last-child, and so the layout the crop measures after the capture).
_OUTLINE_ON_JS = """(e) => {
  const s = e.style, had = e.hasAttribute('style');
  const prev = [had, s.getPropertyValue('outline'), s.getPropertyPriority('outline'),
                s.getPropertyValue('outline-offset'), s.getPropertyPriority('outline-offset')];
  s.setProperty('outline', '3px solid #d32f2f', 'important');
  s.setProperty('outline-offset', '2px', 'important');
  return prev;
}"""
_OUTLINE_OFF_JS = """(e, [had, o, op, f, fp]) => {
  e.style.setProperty('outline', o, op);
  e.style.setProperty('outline-offset', f, fp);
  if (!had && !e.getAttribute('style')) e.removeAttribute('style');
}"""
# A field whose value a screenshot or an observation never shows: the gate's one secret rule.
def _secret_field_js() -> str:
    return "(e) => {" + SECRET_JS + " return hidesSecret(e); }"


class ObservationRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code  # live_view | login_in_progress | page_off_session | stale | no_page | unavailable


def page_view_allowed(url: str, policy: RunPolicy | None) -> bool:
    """This page belongs to this session (design §4): the policy's top-level document rule,
    leases removed."""
    if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
        return False
    if policy is None:
        return False
    viewing = replace(policy, login_lease=None, manual_lease=None)
    decision = decide(RequestMeta("GET", url, resource_type="document", is_navigation=True),
                      viewing)
    return decision.allow and decision.reason in VIEW_REASONS


def _web(url) -> bool:
    return isinstance(url, str) and url.lower().startswith(("https://", "http://"))


def frame_shown(address, href, origin, parent, policy: RunPolicy | None) -> bool:
    """The one rule for a child frame, shared by the observation and the action check (slice
    5m): whether its reading is shown. ``address`` is the frame's address taken before the
    read; ``href`` and ``origin`` are what the reading itself reports; ``parent`` is
    ``(shown, origin)`` of its parent frame's reading, or ``None``.

    Every frame: its parent is shown and its document's origin is not opaque (a frame sandboxed
    without ``allow-same-origin`` never is shown; one with it keeps its own origin, and is judged
    by its address like any other). Then:

    * a frame with a web address (a cross-origin, out-of-process one included): its reading
      reports that same address and the page-view gate admits it;
    * any other (about:blank, about:srcdoc, a document the page wrote, a blob: document) has no
      address of its own: its origin is its parent's, and a web address its reading reports is
      admitted too."""
    if parent is None or not parent[0] or not isinstance(origin, str) or origin in ("", "null"):
        return False
    if _web(address):
        return href == address and page_view_allowed(href, policy)
    return origin == parent[1] and (not _web(href) or page_view_allowed(href, policy))


def child_frames(page) -> list:
    """The page's frames other than its main frame, parents before children (by depth;
    otherwise in the page's own order), detached ones left out."""
    def depth(f):
        n = 0
        while f.parent_frame is not None and n < 1000:
            f, n = f.parent_frame, n + 1
        return n
    out = [f for f in page.frames if f is not page.main_frame and not f.is_detached()]
    return sorted(out, key=depth)


# Fixed extraction code: runs in the isolated world, reads, never writes. One evaluation returns
# the data as JSON plus element handles. It walks the document and every OPEN shadow root.
# Everything is decided with the DOM's own semantics: visibility by checkVisibility (ancestors,
# closed <details>, opacity), editability by isContentEditable / -webkit-user-modify followed up
# through shadow hosts, blocks by the nearest box-rendering, non-inline-level ancestor.
# * Text: per block, its own text nodes concatenated as they stand (a value split across spans
#   or by <wbr> stays whole), one block per line; form and editable content never included. A
#   value the page splits across separate layout boxes is judged box by box (a residual).
#   A text node's visibility is judged through any display:contents parent, on the nearest
#   element that renders a box. Every cut drops the word it would split, so no part of a
#   value that masking would have recognised whole survives a limit.
# * Elements (capped, model-facing): a role from a fixed set; a name from visible, non-editable
#   text only (its own, its <label>, aria-labelledby targets) — never an attribute or a value;
#   what an ordinary field holds (slice 5j: ``holds``), never a secret field's.
# * Mask inventory (uncapped): every form field and editable element with a box (a screenshot
#   masks the secret ones among them), every child frame. ``complete`` is false when a limit
#   was reached.
EXTRACT = r"""() => {
  /*SECRET*/
  const MAX_EL = %(MAX_ELEMENTS)d, MAX_MASK = 20000, MAX_BLOCK_TEXT = 200000, MAX_VALUE = %(MAX_VALUE)d;
  let complete = true;
  const roots = [document];
  for (let i = 0; i < roots.length && roots.length < 5000; i++) {
    for (const e of roots[i].querySelectorAll('*')) if (e.shadowRoot) roots.push(e.shadowRoot);
  }
  if (roots.length >= 5000) complete = false;
  const all = (sel) => roots.flatMap(r => [...r.querySelectorAll(sel)]);
  const box = (e) => { const r = e.getBoundingClientRect(); return r.width >= 1 && r.height >= 1; };
  const seen = (e) => e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const cut = (s, n) => { if (s.length <= n) return s;   // drops the word it would split
    let j = n; while (j > 0 && !/\s/.test(s[j - 1])) j--; return s.slice(0, j); };
  const up = (e) => e.parentElement ||
    ((e.getRootNode && e.getRootNode() instanceof ShadowRoot) ? e.getRootNode().host : null);
  const NOTEXT = /^(INPUT|TEXTAREA|SELECT|OPTION|SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/;
  const editableHere = (e) => e.isContentEditable || NOTEXT.test(e.tagName) ||
    (getComputedStyle(e).getPropertyValue('-webkit-user-modify') || '').startsWith('read-write');
  const editable = (el) => { for (let e = el; e; e = up(e)) if (editableHere(e)) return true; return false; };
  // A display:contents element renders no box of its own: its text is shown when the nearest
  // element that renders one is, and its own (inherited) visibility is visible.
  const shown = (p) => { let c = p;
    while (c && getComputedStyle(c).display === 'contents') c = up(c);
    return !!c && seen(c) && getComputedStyle(p).visibility === 'visible'; };
  const blockOf = (el) => { let b = el;
    while (b) { const d = getComputedStyle(b).display;
      if (!(d.startsWith('inline') || d === 'contents' || !box(b))) break;
      const u = up(b); if (!u) break; b = u; }
    return b; };
  // Visible, non-editable text of a subtree (its open shadow roots included), as it stands.
  const textOf = (root) => { let out = '';
    const scopes = [root];
    for (let i = 0; i < scopes.length; i++) {
      const w = document.createTreeWalker(scopes[i], NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
      while (w.nextNode()) { const n = w.currentNode;
        if (n.nodeType === 1) { if (n.shadowRoot) scopes.push(n.shadowRoot); continue; }
        const p = n.parentElement || up(n); if (!p || editable(p) || !shown(p)) continue;
        out += n.textContent; } }
    return out.replace(/\s+/g, ' ').trim(); };
  const ROLES = new Set(['button','link','checkbox','radio','tab','menuitem','menuitemcheckbox',
    'menuitemradio','option','switch','combobox','textbox','searchbox','slider','spinbutton',
    'listbox','treeitem','gridcell']);
  const tagRole = (e) => {
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea' || e.isContentEditable) return 'textbox';
    if (tag === 'input') return ['checkbox','radio'].includes(type) ? type :
      (['submit','button','reset','image'].includes(type) ? 'button' : 'textbox');
    return 'other';
  };
  // What a field holds (slice 5j): an input's or textarea's value, a select's chosen options,
  // an editable element's text, a checkbox's or radio's state (native, else aria-checked); a
  // secret field (the gate's one rule) holds nothing shown. A long value is cut as text is.
  const NOVALUE = ['submit','button','reset','image','hidden','file','checkbox','radio'];
  const holds = (e) => {
    if (hidesSecret(e)) return {};
    const tag = e.tagName, val = (v) => ({value: v.length > MAX_VALUE ? cut(v, MAX_VALUE) + ' …' : v});
    if (tag === 'INPUT' && (e.type === 'checkbox' || e.type === 'radio')) return {checked: e.checked};
    // an option's label as the page shows it (its label attribute, else its text; r5 Astra)
    if (tag === 'SELECT') return val([...e.selectedOptions].map(o => o.label.replace(/\s+/g, ' ').trim()).join(', '));
    if ((tag === 'INPUT' && !NOVALUE.includes(e.type)) || tag === 'TEXTAREA') return val(e.value);
    if (e.isContentEditable) return val(e.innerText);
    const ac = (e.getAttribute('aria-checked') || '').toLowerCase();
    return ac === 'true' || ac === 'false' ? {checked: ac === 'true'} : {};
  };
  /*CONTROLS*/
  const els = [], data = [];
  const [found, semantic, , room] = actionable(MAX_EL, (e) => {
    const attrRole = (e.getAttribute('role') || '').toLowerCase();
    return !(attrRole && !ROLES.has(attrRole) && tagRole(e) === 'other'); });
  // A select's options take the room the rule kept for them (r1 Terra, Astra): they never
  // displace an element the gate's reading lists.
  let spare = room;
  for (const e of found) {
    const attrRole = (e.getAttribute('role') || '').toLowerCase();
    const role = !semantic.has(e) ? 'clickable' : (ROLES.has(attrRole) ? attrRole : tagRole(e));
    const field = editable(e);
    let name = '';
    const lb = e.getAttribute('aria-labelledby');
    if (lb) name = lb.split(/\s+/).map(i => { const t = document.getElementById(i);
      return t && box(t) && seen(t) && !editable(t) ? textOf(t) : ''; }).join(' ');
    if (!name && e.labels && e.labels.length) name = textOf(e.labels[0]);
    if (!name && !field) name = textOf(e);
    // The kind of a field whose value is not free text (slice 5g): a closed set, from its type;
    // a real <textarea> is multi-line (slice 6b), the one field that takes line breaks.
    const type = e.tagName === 'INPUT' ? (e.getAttribute('type') || '').toLowerCase() : '';
    const textarea = e.localName === 'textarea' && e.namespaceURI === 'http://www.w3.org/1999/xhtml';
    data.push({role, name: cut(name.trim(), 2000), href: e.tagName === 'A' ? e.href : null, field,
               kind: textarea ? %(MULTI_LINE)r : (KINDS.has(type) ? type : null), ...holds(e)});
    els.push(e);
    // A visible select's options, each a target for ``select`` (action model S2-8): an option
    // has no box of its own while the dropdown is closed, so it is listed with its select.
    if (e.tagName === 'SELECT') for (const o of e.options) {
      if (spare <= 0) break;
      spare--;
      data.push({role: 'option', name: cut(o.label.replace(/\s+/g, ' ').trim(), 2000),
                 href: null, field: false});
      els.push(o);
    }
  }
  // Blocks: own text of every block holding visible, non-editable text.
  const blocks = new Map(), full = new Set();
  for (const root of roots) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) {
      const n = w.currentNode, t = n.textContent;
      const p = n.parentElement || up(n);
      if (!p || editable(p) || !shown(p)) continue;
      const b = blockOf(p);
      if (!b || full.has(b)) continue;     // a block that reached its limit takes no more text
      if (!blocks.has(b)) {
        if (!t.trim()) continue;          // whitespace separates; it starts no block
        if (blocks.size >= MAX_MASK) { complete = false; continue; }
        blocks.set(b, '');
      }
      const v = blocks.get(b) + t;
      if (v.length > MAX_BLOCK_TEXT) { complete = false; full.add(b); }
      blocks.set(b, cut(v, MAX_BLOCK_TEXT));
    }
  }
  const text = [...blocks.values()].map(v => v.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const fields = all('*').filter(e => editableHere(e) && !/^(OPTION|SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(e.tagName) && box(e));
  // The mask covers each field's border box. Every piece of editable text must render inside
  // the box of an editable element (a field): its nearest box-rendering element is editable,
  // and the text stays within that box unless the box clips it. Otherwise no screenshot.
  const rg = document.createRange();
  for (const root of roots) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (complete && w.nextNode()) {
      const n = w.currentNode, p = n.parentElement || up(n);
      if (!p || !n.textContent.trim() || !editable(p)) continue;
      rg.selectNodeContents(n);
      const t = rg.getBoundingClientRect();
      if (!(t.width >= 1 || t.height >= 1)) continue;          // not rendered (a script, hidden)
      let c = p; while (c && !box(c)) c = up(c);
      if (!c || !editableHere(c)) { complete = false; break; }
      const bx = c.getBoundingClientRect(), st = getComputedStyle(c);
      const outside = t.left < bx.left - 1 || t.top < bx.top - 1 || t.right > bx.right + 1 || t.bottom > bx.bottom + 1;
      if (outside && (st.overflowX === 'visible' || st.overflowY === 'visible')) complete = false;
    }
  }
  if (fields.length > MAX_MASK) complete = false;
  const frames = all('iframe,frame,embed,object').filter(box);
  return {data: JSON.stringify({href: location.href, origin: self.origin, title: document.title,
                                text: cut(text.join('\n'), 60000), els: data, complete}),
          els, fields: fields.slice(0, MAX_MASK), frames};
}""".replace("/*CONTROLS*/", "const KINDS = new Set(%s);" % json.dumps(sorted(INPUT_KINDS))
            + CONTROLS_JS).replace("/*SECRET*/", SECRET_JS) % {
    "MAX_ELEMENTS": MAX_ELEMENTS, "MULTI_LINE": MULTI_LINE, "MAX_VALUE": MAX_VALUE}


@dataclass
class Extraction:
    href: str
    title: str
    text: str
    elements: list            # model-facing [{role, name, href, field}], capped
    element_handles: list
    field_handles: list       # every form field and editable element with a box (the mask's candidates)
    frame_handles: list
    complete: bool            # False: an inventory limit was reached; no screenshot
    origin: str | None = None  # the document's origin, as the reading reports it (slice 5m)


async def extract(frame) -> Extraction:
    """One evaluation of the fixed extraction code in ``frame`` (isolated world)."""
    handle = await frame.evaluate_handle(EXTRACT)
    try:
        data = json.loads(await (await handle.get_property("data")).json_value())

        async def items(name):
            arr = await handle.get_property(name)
            props = await arr.get_properties()
            return [props[k].as_element() for k in sorted(props, key=lambda x: int(x))
                    if k.isdigit()]

        return Extraction(data["href"], data["title"], data["text"], data["els"],
                          await items("els"), await items("fields"), await items("frames"),
                          bool(data["complete"]), data.get("origin"))
    finally:
        await handle.dispose()


async def has_closed_shadow_root(page) -> bool:
    """Whether the page's document holds a closed shadow root (its content can neither be read
    nor masked). Asked of the browser itself (CDP ``DOM.getDocument`` with ``pierce``); frames
    are not descended (they are blacked out whole). A failure to ask counts as yes."""
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        finally:
            await cdp.detach()
    except Exception:
        return True
    stack = [doc.get("root", {})]
    while stack:
        node = stack.pop()
        for shadow in node.get("shadowRoots", []) or []:
            if shadow.get("shadowRootType") == "closed":
                return True
            stack.append(shadow)
        stack.extend(node.get("children", []) or [])
    return False


ROLE_ALLOWLIST = frozenset({"button", "link", "checkbox", "radio", "tab", "menuitem",
                            "menuitemcheckbox", "menuitemradio", "option", "switch", "combobox",
                            "textbox", "searchbox", "slider", "spinbutton", "listbox", "treeitem",
                            "gridcell", "clickable", "other"})


# What an operator screenshot shows (slice 5f): the captured window cropped to the page's content
# plus a margin, so a small page in the 1920x1080 window is legible on a phone. The content is
# measured right after the capture with the page's animations handled as the capture handles them
# (finite ones finished, infinite ones held at their start while measuring), so it is the layout
# the image shows: every visible text run, image, media, canvas, frame and form control, in the
# document and in every open shadow root (shadow hosts too), and a question's target. The image
# itself is cut in the page's isolated world (a canvas), after the capture's checks passed; when
# anything fails, the whole image is kept. A crop only leaves out window area.
CROP_MARGIN = 24
CROP_MIN = (480, 240)      # Telegram refuses a photo whose sides differ more than 20 times
_CONTENT_BOX_JS = """() => {
  const vw = innerWidth, vh = innerHeight;
  const root = document.documentElement;
  if (!root) return null;
  // the document and every open shadow root, as the capture's own animation handling sees them
  const roots = [document];
  const scopes = [root];
  for (let i = 0; i < scopes.length; i++) {
    for (const e of [scopes[i], ...scopes[i].querySelectorAll('*')]) {
      if (e.shadowRoot) { roots.push(e.shadowRoot); scopes.push(e.shadowRoot); }
    }
  }
  // the layout the capture showed: finite animations finished, infinite ones cancelled (and
  // resumed after measuring), as Playwright's animations: "disabled" does
  const resume = [];
  for (const rt of roots) {
    for (const a of rt.getAnimations()) {
      if (!a.effect || a.playbackRate === 0) continue;
      try {
        if (Number.isFinite(a.effect.getComputedTiming().endTime)) a.finish();
        else if (a.playState !== 'idle') { a.cancel(); resume.push(a); }
      } catch (e) {}
    }
  }
  let l = vw, t = vh, r = 0, b = 0;
  const add = (q) => {
    if (q.width <= 0 || q.height <= 0 || q.right <= 0 || q.bottom <= 0 || q.left >= vw
        || q.top >= vh) return;
    l = Math.min(l, q.left); t = Math.min(t, q.top); r = Math.max(r, q.right);
    b = Math.max(b, q.bottom);
  };
  try {
    const range = document.createRange();
    const kinds = 'img,svg,video,canvas,picture,iframe,frame,object,embed,input,select,textarea,button';
    for (const scope of scopes) {
      const walk = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        if (!n.data.trim()) continue;
        range.selectNodeContents(n);
        for (const q of range.getClientRects()) add(q);
      }
      for (const e of [scope, ...scope.querySelectorAll('*')]) {
        if (!(e instanceof Element)) continue;
        if (e.shadowRoot || e.matches(kinds)) add(e.getBoundingClientRect());
      }
    }
  } finally {
    for (const a of resume) { try { a.play(); } catch (e) {} }
  }
  return r > l && b > t ? [l, t, r, b, vw, vh] : null;
}"""
_CROP_JS = """async ([b64, x, y, w, h]) => {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const bmp = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
  const sx = bmp.width / innerWidth, sy = bmp.height / innerHeight;
  const cw = Math.round(w * sx), ch = Math.round(h * sy);
  const out = new OffscreenCanvas(cw, ch);
  out.getContext('2d').drawImage(bmp, Math.round(x * sx), Math.round(y * sy), cw, ch, 0, 0, cw, ch);
  const data = new Uint8Array(await (await out.convertToBlob({type: 'image/png'})).arrayBuffer());
  let s = '';
  for (let i = 0; i < data.length; i += 0x8000) s += String.fromCharCode(...data.subarray(i, i + 0x8000));
  return btoa(s);
}"""


async def content_clip(page, target=None) -> dict | None:
    """The crop (viewport coordinates) around the page's content and ``target``; ``None`` (the
    whole window) when the content cannot be measured or fills it."""
    try:
        box = await page.evaluate(_CONTENT_BOX_JS)
        if not box:
            return None
        l, t, r, b, vw, vh = (float(v) for v in box)
        if target is not None:
            tb = await target.bounding_box()
            if tb:
                l, t = min(l, tb["x"]), min(t, tb["y"])
                r, b = max(r, tb["x"] + tb["width"]), max(b, tb["y"] + tb["height"])
    except Exception:  # noqa: BLE001 — the crop is presentation; the whole window is the fallback
        return None
    x0, y0 = max(0.0, l - CROP_MARGIN), max(0.0, t - CROP_MARGIN)
    x1, y1 = min(vw, r + CROP_MARGIN), min(vh, b + CROP_MARGIN)
    w, h = max(x1 - x0, min(vw, CROP_MIN[0])), max(y1 - y0, min(vh, CROP_MIN[1]))
    x0, y0 = min(x0, vw - w), min(y0, vh - h)
    if w <= 0 or h <= 0 or (w >= vw and h >= vh):
        return None
    return {"x": int(x0), "y": int(y0), "width": int(round(w)), "height": int(round(h))}


async def crop_png(page, png: bytes, target=None) -> tuple[bytes, tuple[int, int]]:
    """``png`` (just captured) cut to ``content_clip``, and the cut's origin in the window;
    ``png`` itself and ``(0, 0)`` when there is nothing to cut or cutting fails."""
    import base64
    clip = await content_clip(page, target)
    if clip is None:
        return png, (0, 0)
    try:
        out = base64.b64decode(await page.evaluate(_CROP_JS, [
            base64.b64encode(png).decode(), clip["x"], clip["y"], clip["width"],
            clip["height"]]))
    except Exception:  # noqa: BLE001 — the crop is presentation; the whole image is the fallback
        return png, (0, 0)
    if not out.startswith(b"\x89PNG\r\n\x1a\n"):
        return png, (0, 0)
    return out, (clip["x"], clip["y"])


class Observer:
    def __init__(self, registry: HandleRegistry, artifacts: Path, store=None,
                 extractor=extract, token=lambda: secrets.token_urlsafe(12),
                 closed_shadow=has_closed_shadow_root):
        self.closed_shadow = closed_shadow
        self.registry = registry
        self.artifacts = artifacts
        self.store = store
        self.extractor = extractor
        self.token = token
        self.crop_origin = (0, 0)   # where the last stored screenshot began in the window

    async def _extract(self, frame) -> Extraction:
        """The extractor, with every browser-side failure as a typed code: a Patchright error's
        text can carry an address or a selector, so it never travels on."""
        try:
            return await self.extractor(frame)
        except ObservationRefused:
            raise
        except Exception:
            raise ObservationRefused("unavailable") from None

    @staticmethod
    def _check_state(run, session):
        if run.state in ("live", "waiting_live"):
            raise ObservationRefused("live_view")
        if getattr(session, "login_active", False):
            raise ObservationRefused("login_in_progress")
        if session.page is None:
            raise ObservationRefused("no_page")

    @staticmethod
    def _same(session, page, generation, href):
        return session.view_generation == generation and page.url == href

    async def observe(self, run, session, policy: RunPolicy | None) -> Observation:
        observation, _url, _title = await self.observe_page(run, session, policy)
        return observation

    async def observe_page(self, run, session, policy: RunPolicy | None
                           ) -> tuple[Observation, str, str]:
        """``observe``, plus the page's committed top-level address and its raw document
        title as this observation read them (controller-private: the run controller records a
        public-session page as a source from them, Task 12; neither reaches a model)."""
        self._check_state(run, session)
        page = session.page
        generation = session.view_generation
        if not page_view_allowed(page.url, policy):
            raise ObservationRefused("page_off_session")
        main = await self._extract(page.main_frame)
        if main.href != page.url or not page_view_allowed(main.href, policy):
            raise ObservationRefused("page_off_session")
        texts = [main.text]
        parts = [(main, page.main_frame.url)]
        # Each frame under the one rule (``frame_shown``, the check's too), parents first; a
        # frame without a web address of its own is addressed by the one its origin was
        # admitted under (its nearest ancestor's).
        views = {id(page.main_frame): (True, main.origin, page.main_frame.url)}
        for frame in child_frames(page):
            address = frame.url
            parent = views.get(id(frame.parent_frame))
            if _web(address) and not page_view_allowed(address, policy):
                views[id(frame)] = (False, None, None)      # not read at all
                texts.append(FRAME_WITHHELD)
                continue
            try:
                sub = await self.extractor(frame)
            except Exception:
                views[id(frame)] = (False, None, None)
                texts.append(FRAME_WITHHELD)
                continue
            if frame_shown(address, sub.href, sub.origin, parent and parent[:2], policy):
                at = address if _web(address) else parent[2]
                views[id(frame)] = (True, sub.origin, at)
                texts.append(sub.text)
                parts.append((sub, at))
                continue
            views[id(frame)] = (False, sub.origin, None)
            texts.append(FRAME_WITHHELD)
        # The capture's rule for what a field holds (slice 5j): right after the reading, every
        # field whose value or state would be shown is asked the one secret rule again; one that
        # answers secret now, or cannot be asked, shows nothing.
        shown: set[int] = set()
        for ex, _frame_url in parts:
            for info, el in zip(ex.elements, ex.element_handles):
                if "value" in info or "checked" in info:
                    try:
                        if await el.evaluate(_secret_field_js()) is False:
                            shown.add(id(info))
                    except Exception:  # noqa: BLE001 — not asked: not shown
                        pass
        if not self._same(session, page, generation, main.href):
            raise ObservationRefused("stale")
        elements = []
        counts: dict[str, int] = {}
        for ex, frame_url in parts:
            for info, el in zip(ex.elements, ex.element_handles):
                role = str(info.get("role") or "other")
                if role not in ROLE_ALLOWLIST:  # the extraction already maps; this is the floor
                    role = str(info.get("tag_role") or "other")
                    role = role if role in ROLE_ALLOWLIST else "other"
                label = mask_text(info.get("name") or "").text.strip()
                if not label:
                    counts[role] = counts.get(role, 0) + 1
                    label = f"(unnamed {role} {counts[role]})"
                href = info.get("href")
                target = PrivateTarget("link" if href else "element", role, el, href, frame_url)
                handle = self.registry.issue(run.run_id, session.site_id, generation, target)
                kind = info.get("kind")
                value = checked = None
                if id(info) in shown:
                    if isinstance(info.get("value"), str):
                        value = mask_text(info["value"]).text[:MAX_VALUE_SHOWN]
                    if isinstance(info.get("checked"), bool):
                        checked = info["checked"]
                elements.append(ElementView(handle, role, label[:300],
                                            kind if kind in FIELD_KINDS else None, value, checked))
        page_target = PrivateTarget("page", "page", None, None, page.url)
        page_handle = self.registry.issue(run.run_id, session.site_id, generation, page_target)
        text = mask_text("\n".join(t for t in texts if t)).text
        return (Observation(page_handle=page_handle, generation=generation, text=text,
                            elements=tuple(elements), source_id=None), main.href, main.title)

    async def _coverable(self, page, ex: Extraction) -> bool:
        """Whether the mask can cover this reading of the page: it is the page's own address,
        the inventory reached no limit, and no closed shadow root hides content. Asked before
        the capture and again after it, while the marks are on."""
        return ex.href == page.url and ex.complete and not await self.closed_shadow(page)

    async def capture_question(self, run, session, policy: RunPolicy | None,
                               target=None) -> str | None:
        """The question screenshot (action model F6): the real page right before asking, with
        the target outlined (a 3 px border set on its inline style, put back after, applied
        only while capturing), masked as every screenshot is (``_capture``). ``None`` when it
        cannot be taken, a live view or a broker login included."""
        try:
            self._check_state(run, session)
        except ObservationRefused:
            return None
        return await self._capture(run, session, policy, target)

    async def capture_operator(self, run, session, policy: RunPolicy | None) -> str | None:
        """The operator's own screenshot (after an action, ``show_screenshot``): the page as it
        stands, masked as every screenshot is (``_capture``). A live view or a broker login
        raises ``ObservationRefused``; ``None`` when the page cannot be shown (a page that does
        not belong to the session is never captured)."""
        self._check_state(run, session)
        return await self._capture(run, session, policy)

    async def _capture(self, run, session, policy: RunPolicy | None,
                       target=None) -> str | None:
        """Every screenshot's one rule (slice 5h): only password, one-time-code and
        payment-card fields (the gate's one secret rule, ``hidesSecret``) and every child frame
        are masked; every other field shows what it holds. ``None`` when it cannot be faithful:
        the page is not the one read, the reading is incomplete or a closed shadow root hides
        content (``_coverable``, before and again while the marks are on), or a secret field or
        a frame was not covered."""
        page = session.page
        generation = session.view_generation
        if not page_view_allowed(page.url, policy):
            return None
        if target is not None:
            try:
                await target.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                return None
        try:
            ex = await self._extract(page.main_frame)
        except ObservationRefused:
            return None
        if not await self._coverable(page, ex):
            return None

        async def secret_fields(extraction):
            out = []
            for el in extraction.field_handles:
                try:
                    if await el.evaluate(_secret_field_js()):
                        out.append(el)
                except Exception:
                    raise ObservationRefused("unavailable") from None
            return out

        nonce = self.token()
        marked: list = []
        png = None
        outline = None
        try:
            marked = await secret_fields(ex) + list(ex.frame_handles)
            for el in marked:
                await el.evaluate("(e, n) => e.setAttribute('%s', n)" % MASK_ATTR, nonce)
            if target is not None:
                outline = await target.evaluate(_OUTLINE_ON_JS)
            png = await page.screenshot(
                type="png", animations="disabled", caret="hide",
                mask=[page.locator(f'[{MASK_ATTR}="{nonce}"]')], mask_color="#000000",
                timeout=10_000)
            # While the marks are still on, read the page again: every element the mask was
            # meant to cover, and every secret field and frame the page holds now, must carry
            # this capture's mark (one added, replaced or swapped meanwhile was not covered).
            try:
                again = await self._extract(page.main_frame)
            except ObservationRefused:
                again = None
            if again is None or not await self._coverable(page, again):
                png = None
            else:
                for el in marked + await secret_fields(again) + list(again.frame_handles):
                    if not await el.evaluate(
                            "(e, n) => e.isConnected && e.getAttribute('%s') === n" % MASK_ATTR,
                            nonce):
                        png = None
                        break
        except Exception:
            png = None
        finally:
            for el in marked:
                try:
                    await el.evaluate("(e) => e.removeAttribute('%s')" % MASK_ATTR)
                except Exception:
                    pass
            if outline is not None:
                try:
                    await target.evaluate(_OUTLINE_OFF_JS, outline)
                except Exception:
                    pass
        if png is None or not self._same(session, page, generation, ex.href):
            return None
        png, self.crop_origin = await crop_png(page, png, target)
        return self._keep(run, png)

    def _keep(self, run, png: bytes) -> str:
        screenshot_id = "s" + self.token().replace("=", "")
        directory = self.artifacts / "screenshots"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{screenshot_id}.png"
        with open(path, "xb") as fh:
            fh.write(png)
        if self.store is not None:
            self.store.put_screenshot(screenshot_id, run.run_id, str(path))
        return screenshot_id
