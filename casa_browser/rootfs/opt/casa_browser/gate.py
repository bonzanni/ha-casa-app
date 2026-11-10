"""The action gate (action model, design rev 10 §2–§3; plan slice 2): what the check sees of
one browsing action, where the check runs, and what the app compares before it presses.

* ``in_scope`` is design §3's table, rows for rows, over mechanical facts only: the session the
  input is delivered in, the action kind, whether the target is an http(s) link, whether it is
  a search field, how a download is made, and whether a load stays on the host open.
* ``READ`` is the gate's own fixed reading of a page, in the isolated world like
  ``observe.EXTRACT``, whose primitives it copies: numbered blocks with their enclosing
  landmarks and record (table row, list item, article), every actionable control with its
  state, form facts, block, record and heading, and each control's **exact destination**
  (a link's address, a submitter's effective submission destination, a text field's Enter
  destination). Exact destinations stay app-private: a packet carries origin and masked path.
* ``build_packet`` ports the measured packet builder, with the measurement's one finding: the
  target's record (its row) follows its own block, so a cell's button carries its row.
* ``canonical``/``digest``: the packet's canonical UTF-8 JSON and its SHA-256, which binds a
  verdict to the bytes judged.
* ``difference`` is computed over two whole readings, never the capped packets; ``same_reading``
  is the final read's comparison, with no exemption; ``all_controls`` carries it past the list's
  cap, and a ``blind`` reading has no final read.
* ``PendingJudgement`` is one frozen action awaiting its verdict (memory only: the element
  handle cannot be stored).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urlsplit

from casa_browser.masking import looks_random, mask_text

PAGE_CAP = 12_000            # characters of block text in one packet (design §2.2)
MAX_DIFFERENCE = 12_000      # a larger serialised difference has no "same" (design §2.3)
MAX_RECORD_BLOCKS = 200
JUDGEMENT_TTL_S = 180.0      # the check's 90 s timeout twice: the plugin's one retry fits
MAX_NOTE = 600
MAX_JUDGE_PICTURE_BYTES = 2 * 1024 * 1024
MAX_REASON_SHOWN = 300

# The check's reply bounds: the app's own copy of Casa's (``action_judgement.parse_reply``).
# A verdict outside them is no verdict (F1); nothing is clipped.
VERDICTS = {"action": ("go", "ask", "look_first"), "rejudge": ("same", "changed")}
CATEGORIES = ("reading", "local", "account_change", "commitment", "payment", "personal_data",
              "login", "software", "unclear")
GO_CATEGORIES = ("reading", "local")
SCREENSHOTS = ("required", "useful", "none")
MAX_REASON_CHARS = 600
MAX_TEXT_CHARS = 600
MAX_LABEL_CHARS = 32
MAX_OPTION_CHARS = 60
MAX_EXTRA_OPTIONS = 3
MAX_HIGHLIGHTS = 6
MAX_HIGHLIGHT_CHARS = 200

# --------------------------------------------------------------------------- scope (design §3)

FREE_IN_PUBLIC = frozenset({"navigate", "link_click", "back", "search", "search_type",
                            "link_download"})


def scope_kind(action_kind: str, *, is_link: bool = False, search_field: bool = False,
               download_via: str | None = None) -> str:
    """The row of design §3's table an action falls in."""
    if action_kind == "click" and is_link:
        return "link_click"
    if action_kind == "type" and search_field:
        return "search_type"
    if action_kind == "download":
        return "link_download" if download_via == "link" else "button_download"
    return action_kind


def in_scope(session_kind: str, action_kind: str, *, is_link: bool = False,
             search_field: bool = False, download_via: str | None = None,
             same_host: bool = False) -> bool:
    """Whether the check runs (``sends_nothing`` has already taken what only prepares).
    ``session_kind`` is the session the input will be delivered in (for a navigation, the
    destination's), ``"account"`` or ``"public"``. ``scroll`` is free everywhere; on a public
    session navigation, a link click, ``back``, a search, typing into a search field and a link
    download are free; on an account session a navigation to the host of the page open
    (``same_host``, the controller's facts) is free, and never a click, which is a real press;
    everything else is checked."""
    row = scope_kind(action_kind, is_link=is_link, search_field=search_field,
                     download_via=download_via)
    if row == "scroll":
        return False
    if session_kind == "public" and row in FREE_IN_PUBLIC:
        return False
    if session_kind == "account" and same_host and row == "navigate":
        return False
    return True


def sends_nothing(kind: str, target: dict | None, *, enter: bool = False) -> bool:
    """An action that prepares and sends nothing by itself, on any site: typing without Enter,
    choosing a select's value, ticking a checkbox or radio, opening a disclosure (a ``summary``, a
    button with ``aria-expanded``). The check judges what these prepared when something sends it:
    the press or Enter reads the whole form and page. ``target`` is its element's facts; anything
    with a destination (a link, a submitter, a control inside one) always sends."""
    if not target or target.get("secret") or exact_destination(target):
        return False
    if kind == "type":
        return not enter
    if kind == "select":
        return True
    return kind == "click" and (target["role"] in ("checkbox", "radio") or target["tag"] == "summary"
            or (target["role"] == "button" and "expanded" in (target.get("state") or {})))


# --------------------------------------------------------------------------- the reading

# Fixed reading code: runs in the isolated world, reads, never writes. ``box``, ``seen``, ``up``,
# ``editableHere``, ``editable``, ``shown``, ``blockOf`` and ``textOf`` are observe.EXTRACT's,
# verbatim; ``tagRole`` is EXTRACT's with the measured extractor's one addition (an
# ``<input type=search>`` is a ``searchbox``, as the measured packets name it). The rest follows
# the measurement's extractor, plus each block's and
# control's record, a text field's Enter destination and the caps' ``complete``.
# B. The one place that decides whether an element's value is secret: an element (a field, a
# select, a contenteditable, a custom control, any element) typed as a password or whose
# autocomplete names a password, a one-time code or a payment-card field, and everything inside it — its descendants and the open shadow
# trees they host, whatever lies between (secrecy is inherited, never stopped by a non-editable
# layer). Every text and value the gate reads from a page — block texts, control names and states,
# form fields, the submission, a select's options and chosen option, the question screenshot's
# mask — passes through it. Spliced into each script.
SECRET_JS = r"""
  const secretHint = (e) => {
    const t = (e.getAttribute('type') || '').toLowerCase();
    const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
    // the credential and payment tokens of autocomplete: a revealed password field (type=text)
    // still says current-password or new-password
    return t === 'password' || /(^|\s)(current-password|new-password|one-time-code|cc-[a-z-]+)(\s|$)/.test(ac);
  };
  const secretEl = (e) => {
    for (let c = e; c; c = c.parentElement ||
         ((c.getRootNode && c.getRootNode() instanceof ShadowRoot) ? c.getRootNode().host : null))
      if (c.nodeType === 1 && secretHint(c)) return true;
    return false;
  };
  // Inside a secret element, or holding one (open shadow trees included).
  const deepHint = (root) => { const q = [root];
    for (let i = 0; i < q.length; i++) { const n = q[i];
      if (n.nodeType === 1 && secretHint(n)) return true;
      if (n.shadowRoot) q.push(n.shadowRoot);
      q.push(...n.children); }
    return false; };
  const hidesSecret = (e) => secretEl(e) || deepHint(e);
  const NOT_SHOWN = '[not shown]';
  // Every value or label read from an element passes here.
  const valueOf = (e, read) => hidesSecret(e) ? NOT_SHOWN : read();
"""

# Slice 5g: fields whose value the app sets as one in-page step (the browser's own value format:
# keyboard entry into them depends on the locale's display and on which segment a click lands),
# and the kinds an observation names (a number field is typed, its kind still shown). Slice 6b:
# a real <textarea> is shown as ``multi-line``, the one field that takes line breaks.
VALUE_TYPES = frozenset({"time", "date", "datetime-local", "month", "week", "range", "color"})
INPUT_KINDS = VALUE_TYPES | {"number"}
MULTI_LINE = "multi-line"
FIELD_KINDS = INPUT_KINDS | {MULTI_LINE}

# Slice 5g: which elements are actionable — one rule for the observation (``observe.EXTRACT``)
# and the gate's reading (the gate re-finds a target among the controls it reads, design §2.6).
# The semantic controls (``CONTROL_SEL``), and what the page itself makes actionable without
# saying what it is (a custom widget's parts): an element in the tab order (``tabindex`` ≥ 0) or
# with an inline ``onclick``; and, where the page shows its own pointer cursor, each element
# holding its own text there (a list entry, a calendar cell, a menu row; its descendants are part
# of it) and each icon (an ``svg``, an ``img``, or an element with no content). Never a label (its
# control is listed), anything inside a semantic control or holding one.
# One cap for both, a semantic select counting its options (the observation lists them, as many as
# fit). When the semantic controls and their options fit, they all stay and the others take the
# room left, those in view first. When they do not fit, what is in view (the viewport: an open menu
# wherever the page keeps it in its document) goes first, then the rest in page order; leaving out
# a semantic control makes a reading incomplete. The list itself stays in document order. Mechanical facts
# only; nothing is decided by what the text says. Spliced into each script, which define ``all``,
# ``box``, ``seen``, ``up`` and ``shown``. When they do not fit, it also names what a final read
# compares past the list: the semantic ones and those the page marks by attribute, in document
# order and whatever is in view. Never those known by their pointer: a ``:hover`` cursor rule
# changes them as the pointer reaches the target.
# ``pageName`` is what the page itself calls an element that has no visible name, an icon button
# or an image link: a button input's value (what it shows), its ``aria-label``, ``title`` or ``alt``,
# else the ``alt`` of an image it holds, loaded or not: a lazy image has no size until the page is
# scrolled to it, which changes nothing about the control. Never a field's value, and nothing on or
# holding a secret.
# ``ahead``, passed by the observation's search alone (``observe.EXTRACT``), never by ``READ``: given
# the elements in document order, a Map of those to rank before all others, by rank, from ordinals
# matched outside the page. It only changes which elements take the cap's room: the comparable ones
# and every count stay what they are.
CONTROLS_JS = r"""
  const BUTTON_INPUTS = ['submit','button','reset','image'];
  const pageName = (e) => {
    if (hidesSecret(e)) return '';
    const said = (a) => (e.getAttribute(a) || '').trim();
    const pic = e.querySelector('img[alt]:not([alt=""])');
    return (e.tagName === 'INPUT' && BUTTON_INPUTS.includes(e.type) ? e.value.trim() : '')
      || said('aria-label') || said('title') || said('alt')
      || (pic && seen(pic) ? pic.getAttribute('alt').trim() : '');
  };
  const CONTROL_SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role],[contenteditable]';
  const pointer = (e) => getComputedStyle(e).cursor === 'pointer';
  const ownText = (e) => { for (const n of e.childNodes) if (n.nodeType === 3 && n.data.trim()) return shown(e);
    return false; };
  const inView = (e) => { const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth; };
  // [elements in document order, the semantic ones as a Set, whether a semantic one was left out,
  //  the room for their options, how many were left out, the comparable ones or null when all fit]
  const actionable = (cap, accept, ahead) => {
    const order = all('*'), pos = new Map();
    order.forEach((e, i) => pos.set(e, i));
    const base = all(CONTROL_SEL).filter((e) => box(e) && seen(e) && accept(e));
    const baseSet = new Set(base), holders = new Set(), stop = new Set(), extra = [], marked = [];
    for (const b of base) for (let c = up(b); c && !holders.has(c); c = up(c)) holders.add(c);
    for (const e of order) {
      if (baseSet.has(e) || holders.has(e) || e.tagName === 'LABEL' || e.ownerSVGElement) continue;
      let inBase = false, under = false;
      for (let c = up(e); c; c = up(c)) {
        if (baseSet.has(c)) { inBase = true; break; }
        if (stop.has(c)) under = true; }
      if (inBase || !box(e) || !seen(e)) continue;
      const ti = e.getAttribute('tabindex');
      const byAttribute = (ti !== null && /^\s*\d+\s*$/.test(ti)) || e.hasAttribute('onclick');
      if (byAttribute) marked.push(e);   // whatever a pointer rule does to what holds it
      if (under) continue;
      if (byAttribute) { extra.push(e); continue; }
      if (!pointer(e)) continue;
      if (ownText(e)) { extra.push(e); stop.add(e); continue; }
      const tag = e.tagName.toLowerCase();
      if (tag === 'svg' || tag === 'img' || (!e.children.length && !(e.textContent || '').trim())) extra.push(e);
    }
    const cost = (e) => 1 + (e.tagName === 'SELECT' && baseSet.has(e) ? e.options.length : 0);
    const fits = base.reduce((n, e) => n + cost(e), 0) <= cap;
    const first = ahead ? ahead(base.concat(extra).sort((a, b) => pos.get(a) - pos.get(b))) : new Map();
    const rank = (e) => (first.has(e) ? first.get(e) : baseSet.has(e) ? (fits || inView(e) ? 0 : 2) : (inView(e) ? 1 : 3));
    const ranked = base.concat(extra).map((e) => [rank(e), pos.get(e), e]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const els = [];
    let used = 0;
    for (const [, , e] of ranked) {
      const room = cap - used;
      if (room <= 0) break;
      // A select with more options than the room stays, with half of it; others can still fit.
      used += cost(e) <= room ? cost(e) : Math.ceil(room / 2);
      els.push(e);
    }
    els.sort((a, b) => pos.get(a) - pos.get(b));
    const kept = new Set(els);
    return [els, baseSet, base.some((e) => !kept.has(e)), cap - els.length, ranked.length - els.length,
            fits ? null : base.concat(marked).sort((a, b) => pos.get(a) - pos.get(b))];
  };
"""

READ = r"""([target, SALT]) => {
  /*SECRET*/
  // SHA-256 of a string's UTF-8 bytes (crypto.subtle is async and absent on plain-http pages).
  const sha256 = (msg) => {
    const bytes = new TextEncoder().encode(msg), K = [], H = [];
    const frac = (x) => ((x - Math.floor(x)) * 4294967296) | 0;
    for (let n = 2, found = 0; found < 64; n++) {
      let prime = true; for (let d = 2; d * d <= n; d++) if (n % d === 0) { prime = false; break; }
      if (prime) { if (found < 8) H[found] = frac(Math.pow(n, 1 / 2)); K[found++] = frac(Math.pow(n, 1 / 3)); } }
    const l = bytes.length, total = ((l + 72) >> 6) << 6;
    const buf = new Uint8Array(total); buf.set(bytes); buf[l] = 0x80;
    const dv = new DataView(buf.buffer);
    dv.setUint32(total - 4, (l * 8) >>> 0); dv.setUint32(total - 8, Math.floor(l / 536870912));
    const w = new Int32Array(64), r = (x, k) => (x >>> k) | (x << (32 - k));
    for (let off = 0; off < total; off += 64) {
      for (let i = 0; i < 16; i++) w[i] = dv.getInt32(off + 4 * i);
      for (let i = 16; i < 64; i++) {
        const a = w[i - 15], b = w[i - 2];
        w[i] = (w[i - 16] + (r(a, 7) ^ r(a, 18) ^ (a >>> 3)) + w[i - 7] + (r(b, 17) ^ r(b, 19) ^ (b >>> 10))) | 0; }
      let [a, b, c, d, e, f, g, h] = H;
      for (let i = 0; i < 64; i++) {
        const t1 = (h + (r(e, 6) ^ r(e, 11) ^ r(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
        const t2 = ((r(a, 2) ^ r(a, 13) ^ r(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0; }
      [a, b, c, d, e, f, g, h].forEach((x, i) => { H[i] = (H[i] + x) | 0; }); }
    return H.map(x => (x >>> 0).toString(16).padStart(8, '0')).join('');
  };
  const MAX_EL = 400, MAX_BLOCKS = 20000, MAX_BLOCK_TEXT = 200000;
  let blind = false;      // short for a cause other than the control cap
  const roots = [document];
  for (let i = 0; i < roots.length && roots.length < 5000; i++) {
    for (const e of roots[i].querySelectorAll('*')) if (e.shadowRoot) roots.push(e.shadowRoot);
  }
  if (roots.length >= 5000) blind = true;
  const all = (sel) => roots.flatMap(r => [...r.querySelectorAll(sel)]);
  const box = (e) => { const r = e.getBoundingClientRect(); return r.width >= 1 && r.height >= 1; };
  const seen = (e) => e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const up = (e) => e.parentElement ||
    ((e.getRootNode && e.getRootNode() instanceof ShadowRoot) ? e.getRootNode().host : null);
  const NOTEXT = /^(INPUT|TEXTAREA|SELECT|OPTION|SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/;
  const editableHere = (e) => e.isContentEditable || NOTEXT.test(e.tagName) ||
    (getComputedStyle(e).getPropertyValue('-webkit-user-modify') || '').startsWith('read-write');
  const editable = (el) => { for (let e = el; e; e = up(e)) if (editableHere(e)) return true; return false; };
  const shown = (p) => { let c = p;
    while (c && getComputedStyle(c).display === 'contents') c = up(c);
    return !!c && seen(c) && getComputedStyle(p).visibility === 'visible'; };
  const blockOf = (el) => { let b = el;
    while (b) { const d = getComputedStyle(b).display;
      if (!(d.startsWith('inline') || d === 'contents' || !box(b))) break;
      const u = up(b); if (!u) break; b = u; }
    return b; };
  const textOf = (root) => { let out = '';
    const scopes = [root];
    for (let i = 0; i < scopes.length; i++) {
      const w = document.createTreeWalker(scopes[i], NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
      while (w.nextNode()) { const n = w.currentNode;
        if (n.nodeType === 1) { if (n.shadowRoot) scopes.push(n.shadowRoot); continue; }
        const p = n.parentElement || up(n); if (!p || editable(p) || secretEl(p) || !shown(p)) continue;
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
      (['submit','button','reset','image'].includes(type) ? 'button' :
       (type === 'search' ? 'searchbox' : 'textbox'));
    return 'other';
  };
  const isCc = (e) => /(^|\s)cc-/.test((e.getAttribute('autocomplete') || '').toLowerCase());
  const LANDMARK = 'form,main,nav,aside,header,footer,article,dialog,section,[role=dialog],[role=region],[role=main],[role=navigation],[role=form],[role=complementary],[role=banner],[role=contentinfo],[role=alertdialog]';
  const RECORD = 'tr,[role=row],li,[role=listitem],article';
  const closestAcross = (el, sel) => { for (let e = el; e; e = up(e)) if (e.matches && e.matches(sel)) return e; return null; };
  const ids = new Map(); let nextId = 1;
  const idOf = (e) => { if (!e) return null; if (!ids.has(e)) ids.set(e, nextId++); return ids.get(e); };

  // Blocks, in document order: own text of every block holding visible, non-editable text.
  const blocks = new Map(), full = new Set();
  for (const root of roots) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) {
      const n = w.currentNode, t = n.textContent;
      const p = n.parentElement || up(n);
      if (!p || editable(p) || secretEl(p) || !shown(p)) continue;
      const b = blockOf(p);
      if (!b || full.has(b)) continue;
      if (!blocks.has(b)) {
        if (!t.trim()) continue;
        if (blocks.size >= MAX_BLOCKS) { blind = true; continue; }
        blocks.set(b, '');
      }
      const v = blocks.get(b) + t;
      if (v.length > MAX_BLOCK_TEXT) { blind = true; full.add(b); continue; }
      blocks.set(b, v);
    }
  }
  const blockList = [];
  for (const [b, v] of blocks) {
    const text = v.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const cont = [];
    for (let e = b; e; e = up(e)) if (e.matches && e.matches(LANDMARK)) cont.push(idOf(e));
    blockList.push({el: b, text, containers: cont, record: idOf(closestAcross(b, RECORD))});
  }
  const blockIndexOf = (el) => {
    const own = blockOf(el);
    for (let i = 0; i < blockList.length; i++) if (blockList[i].el === own) return i;
    let deep = -1;
    for (let i = 0; i < blockList.length; i++) { const b = blockList[i].el;
      if (b === el || b.contains(el)) { if (deep < 0 || blockList[deep].el.contains(b)) deep = i; } }
    if (deep >= 0) return deep;
    let best = -1;
    for (let i = 0; i < blockList.length; i++) {
      const pos = blockList[i].el.compareDocumentPosition(el);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) best = i;
    }
    return best;
  };
  const headings = all('h1,h2,h3,h4,h5,h6,[role=heading]').filter(h => box(h) && seen(h));
  const headingOf = (el) => {
    let best = null;
    for (const h of headings) {
      const pos = h.compareDocumentPosition(el);
      if ((pos & Node.DOCUMENT_POSITION_FOLLOWING) || (pos & Node.DOCUMENT_POSITION_CONTAINED_BY)) best = h;
    }
    return best ? textOf(best) : null;
  };
  const labelOf = (e) => {
    let name = '';
    const lb = e.getAttribute('aria-labelledby');
    if (lb) name = lb.split(/\s+/).map(i => { const t = document.getElementById(i);
      return t && box(t) && seen(t) && !editable(t) ? textOf(t) : ''; }).join(' ');
    if (!name && e.labels && e.labels.length) name = textOf(e.labels[0]);
    return name.trim();
  };
  const stateOf = (e) => {
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    const st = {};
    const label = (o) => o.label.replace(/\s+/g,' ').trim();     // the name the option shows
    // slice 5j (r5): a secret control's state passes the one rule too, as every value does
    if (tag === 'input' && (type === 'checkbox' || type === 'radio')) st.checked = valueOf(e, () => e.checked);
    else if (tag === 'select' && hidesSecret(e)) st.selected = NOT_SHOWN;
    else if (tag === 'select' && e.multiple) {      // every chosen option, not only the first
      st.selected = [...e.selectedOptions].map(label);
      st.options = [...e.options].map(label); }
    else if (tag === 'select') { const o = e.selectedOptions && e.selectedOptions[0];
      st.selected = o ? label(o) : null;
      st.options = [...e.options].map(label); }
    else if ((tag === 'input' && !['submit','button','reset','image','hidden'].includes(type)) || tag === 'textarea') {
      st.value = valueOf(e, () => e.value); }
    else if (e.isContentEditable) st.value = valueOf(e, () => e.innerText);
    if (!hidesSecret(e)) for (const a of ['aria-pressed','aria-checked','aria-expanded','aria-selected']) {
      const v = e.getAttribute(a); if (v !== null) st[a.slice(5)] = v; }
    if (e.disabled) st.disabled = true;
    return st;
  };
  // Submission facts are the browser's own: the IDL getters (an empty or missing action is the
  // document's address, an invalid method is GET), read through the form prototype so a control
  // named "action" cannot shadow them; a button's type is its IDL type (an invalid one submits).
  // The element whose activation a press on ``e`` runs: the nearest flat-tree ancestor (``e``
  // included) with activation behaviour, a label forwarding to its control. Its facts — a
  // link's address, a submitter's submission — are what the press does.
  const ACTIVE = 'a[*|href],area[href],button,input:not([type=hidden]),label,details>summary:first-of-type';
  // Chromium hands a click on up when the inner control does not take it (measured: a type=button or
  // a text field inside a link follows it, an input inside a submit button submits). So an enclosing
  // link or submit button wins over a nearer control.
  const activatorOf = (e) => {
    let first = null;
    for (let c = e; c; c = c.assignedSlot || c.parentElement ||
         ((c.getRootNode && c.getRootNode() instanceof ShadowRoot) ? c.getRootNode().host : null))
      if (c.matches && c.matches(ACTIVE)) {
        const a = c.tagName === 'LABEL' ? (c.control || c) : c;
        if (a.matches('a[*|href]') || isSubmitter(a)) return a;
        first = first || a;
      }
    return first || e;
  };
  // A link's address, an SVG <a>'s too (its href is an SVGAnimatedString; a broken one still is one).
  const linkUrl = (a) => { const h = a.href;
    if (!h || h.baseVal === undefined) return h;
    try { return new URL(h.baseVal, a.baseURI).href; } catch (_) { return h.baseVal; } };
  const isSubmitter = (e) => {
    const tag = e.tagName.toLowerCase();
    if (tag === 'button') return e.type === 'submit' && !!e.form;
    if (tag === 'input') return (e.type === 'submit' || e.type === 'image') && !!e.form;
    return false;
  };
  const FORM = (p) => Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, p).get;
  const formEls = (f) => FORM('elements').call(f);   // a control named "elements" cannot shadow it
  const actOf = (f) => FORM('action').call(f);
  const methodOf = (f) => FORM('method').call(f);
  const targetOf = (f) => FORM('target').call(f);
  const subAction = (d, f) => d.hasAttribute('formaction') ? d.formAction : actOf(f);
  const subMethod = (d, f) => d.hasAttribute('formmethod') ? d.formMethod : methodOf(f);
  const subTarget = (d, f) => d.hasAttribute('formtarget') ? d.formTarget : targetOf(f);
  // A. The submission the browser itself would build for a form and a submitter (or none): its
  // method, resolved action, enctype, target and encoding, the submitter, and the entries of
  // new FormData(form, submitter) — the browser's own entry list (disabled, unchecked and
  // unselected controls left out; every chosen option of a multiple select; the submitter's own
  // name and value). Two forms of it: ``raw``, a salted SHA-256 of all of it unmasked, which is
  // what every comparison binds (masking is for display only and can never hide a change), and
  // the shown one. In a form that holds a secret (B), or a custom control whose content the gate
  // cannot read, no entry's value is shown, only its name. ``typed`` is a text field being typed
  // into: its own entry is a marker in both forms, so typing alone changes nothing.
  // Constructing the FormData fires the page's own "formdata" event, as a submission does.
  const BLOCKING = ['text','search','url','tel','email','password','date','month','week','time','datetime-local','number'];
  const defaultButton = (f) => [...f.getRootNode().querySelectorAll('button,input')].find(x => x.form === f && isSubmitter(x));
  const holdsSecret = (f, s) => deepHint(f) || (s && hidesSecret(s)) ||
    [...formEls(f)].some(x => hidesSecret(x) || x.localName.includes('-'));
  const submissionOf = (f, s, typed) => {
    // Native validation without firing its events: an invalid form is not submitted.
    const noValidate = FORM('noValidate').call(f) || (s && s.formNoValidate);
    if (!noValidate && ![...formEls(f)].every(x => !x.willValidate || x.validity.valid))
      return {none: 'invalid_form'};
    // A file input holding a file: its content cannot be bound, the step is not judged.
    if ([...formEls(f)].some(x => x.type === 'file' && x.files && x.files.length)) return {unreadable: 'file'};
    let fd;
    try { fd = new FormData(f, s || null); } catch (_) { return {unreadable: 'formdata'}; }
    const hide = holdsSecret(f, s);
    let marked = false;
    const raw = [], entries = [];
    for (const [k, v] of fd.entries()) {
      if (typeof v !== 'string') {
        if (v.name || v.size) return {unreadable: 'file'};   // a chosen file: not bound, not judged
        raw.push([k, '[no file]']); entries.push([k, '[no file]']); continue;
      }
      if (typed && !marked && k === typed.name && v === typed.value) {
        raw.push([k, '[the typed field]']); entries.push([k, hide ? NOT_SHOWN : '[the typed field]']); marked = true; continue; }
      raw.push([k, v]);
      entries.push([k, hide ? NOT_SHOWN : v]);
    }
    const baseTarget = (document.querySelector('base[target]') || {target: ''}).target;
    const subName = s ? (labelOf(s) || (s.tagName === 'INPUT' ? (s.value || s.alt || '') : textOf(s))) : '';
    const out = {method: s ? subMethod(s, f) : methodOf(f), action: s ? subAction(s, f) : actOf(f),
            enctype: (s && s.hasAttribute('formenctype')) ? s.formEnctype : FORM('enctype').call(f),
            target: (s ? subTarget(s, f) : targetOf(f)) || baseTarget,
            submitter: s ? {role: tagRole(s), name: valueOf(s, () => subName.slice(0, 300))} : null,
            entries};
    // The encoding's inputs, not a resolution of them: any change in either is a change.
    out.raw = sha256(SALT + JSON.stringify([out.method, out.action, out.enctype, out.target,
      FORM('acceptCharset').call(f), document.characterSet,
      s ? [tagRole(s), subName, s.name, s.value] : null, raw]));
    return out;
  };
  // What Enter in an input submits: through the form's default button (in the form's own tree;
  // a disabled one means Enter submits nothing), else, with no submit button, the form itself
  // when at most one field blocks implicit submission. null: Enter submits nothing (not an
  // input of a type whose Enter submits, no form); {none: …}: the form has a submit path but
  // Enter does not take it.
  const enterSubmission = (e) => {
    if (e.tagName !== 'INPUT' || !BLOCKING.includes(e.type) || !e.form) return null;
    const f = e.form, d = defaultButton(f);
    if (d) return d.matches(':disabled') ? {none: 'disabled_default_button'} : submissionOf(f, d, e);
    const blocking = [...formEls(f)].filter(x => x.tagName === 'INPUT' && BLOCKING.includes(x.type)).length;
    return blocking > 1 ? {none: 'no_implicit_submission'} : submissionOf(f, null, e);
  };
  const formFacts = (f) => {
    if (!f) return null;
    const fields = [];
    for (const x of formEls(f)) {
      const type = (x.getAttribute('type') || x.tagName).toLowerCase();
      if (type === 'hidden' || type === 'fieldset') continue;
      if (['submit','button','reset','image'].includes(type) || x.tagName === 'BUTTON') continue;
      let lab = labelOf(x);
      if (!lab && x.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio') && x.parentElement) lab = textOf(x.parentElement);
      const f2 = {label: lab || '(unnamed field)', type: x.tagName === 'SELECT' ? 'select' : (x.tagName === 'TEXTAREA' ? 'textarea' : type)};
      const st = stateOf(x);
      if ('value' in st) f2.value = st.value; if ('checked' in st) f2.checked = st.checked; if ('selected' in st) f2.selected = st.selected;
      fields.push(f2);
    }
    return {id: idOf(f), method: methodOf(f), action: actOf(f), target: targetOf(f), fields};
  };
  const TEXT_TYPES = ['', 'text', 'search', 'email', 'tel', 'url', 'number'];
  const editableText = (e) => {
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    if (tag === 'input') return TEXT_TYPES.includes(type);
    return tag === 'textarea' || e.isContentEditable;
  };
  // What two readings can compare of a control (``COMPARED``, and ``is_submitter``): no id of one
  // read, and nothing that walks the page's blocks, as ``all_controls`` asks it of every control.
  const factsOf = (e) => {
    const attrRole = (e.getAttribute('role') || '').toLowerCase();
    const role = ROLES.has(attrRole) ? attrRole : tagRole(e);
    const field = editable(e);
    let name = labelOf(e);
    if (!name && !field) name = textOf(e);
    if (!name) name = pageName(e);
    const tag = e.tagName.toLowerCase();
    const form = e.form || closestAcross(e, 'form');
    const act = activatorOf(e), sub = isSubmitter(act);
    const c = {role, name: name.slice(0, 2000), tag,
      input_type: (e.getAttribute('type') || '').toLowerCase() || null,
      href: tag === 'a' ? linkUrl(e) : null,
      activator_href: (act !== e && act.localName === 'a') ? linkUrl(act) : null,
      is_submitter: sub,
      submit_action: sub ? subAction(act, act.form) : null,
      submit_method: sub ? subMethod(act, act.form) : null,
      submit_target: sub ? subTarget(act, act.form) : null,
      state: stateOf(e)};
    // A text field's Enter submits through its form's default button: the first submit button
    // in tree order whose form owner is the form (an image input included, which form.elements
    // omits). Every text-like input qualifies, whatever its role attribute.
    if (form && (tag === 'input' ? editableText(e) : (role === 'textbox' || role === 'searchbox'))) {
      // The default button lives in the form's own tree (a document or a shadow root).
      const d = [...form.getRootNode().querySelectorAll('button,input')].find(x => x.form === form && isSubmitter(x));
      c.enter_action = d ? subAction(d, form) : actOf(form);
      c.enter_method = d ? subMethod(d, form) : methodOf(form);
      c.enter_target = d ? subTarget(d, form) : targetOf(form);
    }
    return c;
  };
  const controlOf = (e, idx) => {
    const c = factsOf(e), form = e.form || closestAcross(e, 'form');
    return Object.assign(c, {idx,
      form: formFacts(form),
      container: idOf(closestAcross(e, LANDMARK)),
      record: idOf(closestAcross(e, RECORD)),
      block: blockIndexOf(e),
      heading: headingOf(e),
      secret: hidesSecret(e),
      download_attr: c.tag === 'a' && e.hasAttribute('download'),
      form_self: form ? [...formEls(form)].filter(x => { const t = (x.getAttribute('type') || x.tagName).toLowerCase();
        return !(t === 'hidden' || t === 'fieldset' || ['submit','button','reset','image'].includes(t) || x.tagName === 'BUTTON'); }).indexOf(e) : -1,
      editable_text: editableText(e),
      search_field: c.role === 'searchbox' || (c.tag === 'input' && (e.getAttribute('type') || '').toLowerCase() === 'search')});
  };
  /*CONTROLS*/
  const controls = [], els = [];
  const [found, semantic, over, , , comparable] = actionable(MAX_EL, (e) => {
    const attrRole = (e.getAttribute('role') || '').toLowerCase();
    return !(attrRole && !ROLES.has(attrRole) && tagRole(e) === 'other'); });
  // Overflowed: every comparable control, as one salted digest of what it is and does.
  const allControls = comparable && sha256(SALT + JSON.stringify(comparable.map((e) => {
    const c = factsOf(e);
    return /*COMPARED*/.map((k) => c[k]); })));
  for (const e of found) {
    const c = controlOf(e, controls.length);
    if (!semantic.has(e)) c.role = 'clickable';
    controls.push(c);
    els.push(e);
  }
  let tIdx = -1, tFacts = null;
  if (target) {
    tIdx = els.indexOf(target);
    tFacts = tIdx >= 0 ? controls[tIdx] : (target.isConnected ? controlOf(target, -1) : null);
    if (tFacts) { tFacts = Object.assign({}, tFacts, {connected: target.isConnected,
      visible: box(target) && seen(target), enabled: !target.disabled,
      submission: isSubmitter(activatorOf(target)) ? submissionOf(activatorOf(target).form, activatorOf(target), null) : null,
      enter_submission: enterSubmission(target)}); }
  }
  const hasCc = all('input,select,textarea').some(isCc);
  return {data: JSON.stringify({href: location.href, origin: self.origin, title: document.title,
    complete: !blind && !over, blind: blind || undefined, all_controls: allControls || undefined,
    blocks: blockList.map(b => ({text: b.text, containers: b.containers, record: b.record})),
    controls, target: tFacts, target_idx: tIdx, has_cc: hasCc}), els};
}"""


# The key of every submission's ``raw`` digest: a secret value never leaves the page, yet any
# change in it changes the digest; unknown outside this process.
_SALT = secrets.token_hex(16)


@dataclass
class Reading:
    """One reading of a page (``READ``'s data): ``href``, ``title``, ``complete``, ``blocks``,
    ``controls``, ``target``, ``target_idx``, ``has_cc``, and only when they apply ``blind`` and
    ``all_controls``. Exact destinations are app-private."""

    data: dict
    handles: list | None = field(default=None, repr=False)

    @property
    def complete(self) -> bool:
        return bool(self.data.get("complete"))

    @property
    def target(self) -> dict | None:
        return self.data.get("target")


# What ``all_controls`` binds of each control, in ``READ``'s digest and ``listed_digest`` alike.
COMPARED = ("role", "name", "tag", "input_type", "state", "href", "activator_href",
            "submit_action", "submit_method", "submit_target",
            "enter_action", "enter_method", "enter_target")
FRAME_WITHHELD = "[a frame on this page is not shown]"
_ID_STRIDE = 1_000_000       # keeps each frame's landmark and record ids apart


def merge_frame(base: dict, other: dict, k: int, *, shown: bool) -> None:
    """Add another frame's reading to a page reading. A frame whose own address the page-view
    gate does not admit shows only a placeholder block, and its reading enters the comparison
    as a digest (``frames_withheld``), so any change in it is a difference."""
    base["complete"] = bool(base.get("complete")) and bool(other.get("complete"))
    base["has_cc"] = bool(base.get("has_cc")) or bool(other.get("has_cc"))
    if other.get("blind"):
        base["blind"] = True
    if not shown:
        base["blocks"].append({"text": FRAME_WITHHELD, "containers": [], "record": None})
        base.setdefault("frames_withheld", []).append(digest(canonical(other)))
        return
    off = _ID_STRIDE * k
    shift = lambda v: None if v is None else v + off  # noqa: E731
    nb, nc = len(base["blocks"]), len(base["controls"])
    for b in other.get("blocks") or ():
        base["blocks"].append({"text": b["text"], "containers": [c + off for c in b["containers"]],
                               "record": shift(b.get("record"))})
    for c in other.get("controls") or ():
        c = dict(c, idx=nc + c["idx"], record=shift(c.get("record")),
                 container=shift(c.get("container")), frame_href=other.get("href"))
        if c.get("form"):
            c["form"] = dict(c["form"], id=shift(c["form"].get("id")))
        c["block"] = nb + c["block"] if c.get("block") is not None and c["block"] >= 0 else -1
        base["controls"].append(c)


def listed_digest(frame_data: dict) -> str:
    """An uncapped frame's ``all_controls`` value beside a capped one: all it has is listed."""
    return digest(canonical([[c.get(k) for k in COMPARED]
                             for c in frame_data.get("controls") or ()]))


async def read(frame, target=None, *, with_handles: bool = False) -> Reading:
    """One evaluation of ``READ`` in ``frame`` (isolated world), optionally about ``target``."""
    handle = await frame.evaluate_handle(READ, [target, _SALT])
    try:
        data = json.loads(await (await handle.get_property("data")).json_value())
        handles = None
        if with_handles:
            arr = await handle.get_property("els")
            props = await arr.get_properties()
            handles = [props[k].as_element() for k in sorted(props, key=lambda x: int(x))
                       if k.isdigit()]
        return Reading(data, handles)
    finally:
        await handle.dispose()


# --------------------------------------------------------------------------- packets (§2.2)

UUIDISH = re.compile(r"^[0-9a-f]{8,}(-[0-9a-f]{4,})*$", re.I)


def masked_path(url: str) -> str:
    """The page path with query values dropped and maskable segments masked (the measured
    packets' ``masked_path``)."""
    p = urlsplit(url)
    segs = []
    for s in p.path.split("/"):
        if s and (looks_random(s) or UUIDISH.match(s) or re.search(r"\d{6,}", s)):
            segs.append("[masked]")
        else:
            segs.append(s)
    return "/".join(segs) or "/"


def _site_of(host: str | None) -> str:
    return ".".join((host or "").split(".")[-2:])


def address_name(url: str) -> str:
    """An address as an event names a step: host and masked path, no scheme and no query."""
    try:
        return f"{urlsplit(url).hostname or ''}{masked_path(url)}"
    except ValueError:
        return ""


def same_host_load(url: str, page_url: str) -> bool:
    """``url`` loads another page of the host of the open web page ``page_url``: the same hostname,
    so a subdomain or a neighbour under one public suffix is another host. A caller holds the
    destination to its own session's site as well. The open page's own address with a fragment loads
    nothing: it only tells the page's script."""
    try:
        here, there = urlsplit(page_url), urlsplit(url)
        hosts = here.hostname, there.hostname
    except ValueError:
        return False
    return (here.scheme in ("http", "https") and there.scheme in ("http", "https") and all(hosts)
            and hosts[0] == hosts[1]
            and not (there.fragment and urldefrag(url).url == urldefrag(page_url).url))


def destination(url: str | None, page_url: str) -> dict:
    """What a packet says of an address: origin, masked path, same site or not."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return {"scheme": (url or "").split(":")[0] or None}
    p = urlsplit(url)
    return {"origin": f"{p.scheme}://{p.hostname}{':' + str(p.port) if p.port else ''}",
            "path": masked_path(url),
            "same_site": _site_of(p.hostname) == _site_of(urlsplit(page_url).hostname)}


def _mt(s: str | None) -> str:
    return mask_text(s or "").text


NOT_SHOWN = "[not shown]"


def _shown(v):
    return v if v == NOT_SHOWN else _mt(v)


def _masked(v):
    """Any page text a packet shows, masked: a string, or each string of a list."""
    if isinstance(v, str):
        return _shown(v)
    if isinstance(v, list):
        return [_masked(x) for x in v]
    return v


def _whole(v):
    """A field's value as the one place a packet shows it: masked, never cut, so a long text
    typed or pasted into it reaches the check entire; a secret value stays ``[not shown]``."""
    return v if v == NOT_SHOWN else _mf(v)


def masked_state(st: dict | None, *, cut_options: bool = False, whole_value: bool = False) -> dict:
    """A control's state as a packet shows it: every text masked (a value also cut to 200
    characters, unless ``whole_value``; in a control line a select's options to 12); a secret
    value stays ``[not shown]``."""
    out = {}
    for k, v in (st or {}).items():
        if cut_options and k == "options" and isinstance(v, list):
            v = v[:12]
        if k == "value" and isinstance(v, str):
            out[k] = _whole(v) if whole_value else _shown(v)[:200]
        else:
            out[k] = _masked(v)
    return out


def in_target_form(c: dict, target: dict | None) -> bool:
    """Whether the packet's ``target.form.fields`` lists this control: one of the fields of the
    target's own form, in the target's frame (``merge_frame`` keeps other frames' ids apart)."""
    tf, cf = (target or {}).get("form"), c.get("form")
    return bool(tf and cf and cf.get("id") == tf.get("id") and c.get("form_self", -1) >= 0)


def control_line(c: dict, *, whole_value: bool = False) -> str:
    """A control as ``page.controls`` lists it. ``whole_value``: its value is shown whole here,
    because no other member of the packet shows it."""
    name = _mt(c.get("name")).strip() or "(unnamed)"
    st = masked_state(c.get("state"), cut_options=True, whole_value=whole_value)
    s = f'{c["role"]}: {name[:200]}'
    if st:
        s += " " + json.dumps(st, ensure_ascii=False)
    return s


def block_order(blocks: list, target: dict | None) -> list[int]:
    """The order blocks enter a packet: the target's own block; then the other blocks of its
    record (table row, list item or article), all of them, in document order; then its enclosing form or
    landmark; then the rest by distance."""
    if target is None or target.get("block") is None or target["block"] < 0:
        return list(range(len(blocks)))
    tb = target["block"]
    record = target.get("record")
    cont = target.get("container")
    in_record = [i for i, b in enumerate(blocks)
                 if i != tb and record is not None and b.get("record") == record]
    in_record = in_record[:MAX_RECORD_BLOCKS]          # in document order, as the row reads
    taken = {tb, *in_record}
    inside = [i for i, b in enumerate(blocks)
              if i not in taken and cont is not None and cont in (b.get("containers") or ())]
    taken.update(inside)
    rest = [i for i in range(len(blocks)) if i not in taken]
    return ([tb] + in_record + sorted(inside, key=lambda i: abs(i - tb))
            + sorted(rest, key=lambda i: abs(i - tb)))


def build_packet(reading: dict, target: dict | None, action: dict, *, task: str, session: str,
                 site: str, run_so_far=(), download: dict | None = None,
                 plan: str | None = None, approved: str | None = None) -> dict:
    """The packet of design §2.2, exactly the measured members, from one reading (``READ``'s
    data, or a measurement capture of the same shape). ``target`` is the target's control
    facts, or None for a page-level action (``back``). ``session`` is ``signed_in`` or
    ``public``. ``plan`` and ``approved`` are members only when present."""
    page_url = reading["href"]
    blocks = reading["blocks"]
    controls = reading["controls"]
    order = block_order(blocks, target)
    out, used, truncated, number = [], 0, False, {}
    for i in order:
        text = _mt(blocks[i]["text"])
        if used + len(text) > PAGE_CAP:
            truncated = True
            break
        out.append({"n": len(out) + 1, "text": text})
        number[i] = len(out)
        used += len(text)
    pkt: dict[str, Any] = {
        "task": task,
        "where": {"session": session, "site": site, "host": urlsplit(page_url).hostname,
                  "title": _mt(reading.get("title")), "path": masked_path(page_url)},
        # The chosen option's label is page text: masked like every other (a card select is
        # never chosen by the agent, so its choice never reaches a packet).
        "action": ({**action, "option": _shown(action["option"])}
                   if isinstance(action.get("option"), str) else action)}
    t = target
    if t is not None:
        has_block = t.get("block") is not None and t["block"] >= 0 and t["block"] in number
        tgt: dict[str, Any] = {
            "block": number[t["block"]] if has_block else None,
            "record_blocks": sorted(number[i] for i, b in enumerate(blocks)
                                    if i in number and i != t.get("block")
                                    and t.get("record") is not None
                                    and b.get("record") == t.get("record")),
            "role": t["role"], "name": _mt(t["name"]).strip()[:300] or f"(unnamed {t['role']})",
            "tag": t["tag"], "input_type": t["input_type"], "is_link": bool(t.get("href"))}
        if t.get("href") or t.get("activator_href"):
            tgt["destination"] = destination(t.get("href") or t["activator_href"], page_url)
            if t.get("download_attr"):
                tgt["download_attribute"] = True
        if t.get("state"):
            tgt["state"] = masked_state(t["state"])
        f = t.get("form")
        if f:
            # A field's value is whole here, and only here: what a press sends of a list pasted
            # into the form is what the check reads.
            tgt["form"] = {"method": f["method"], "destination": destination(f["action"], page_url),
                           "fields": [{k: (v if k == "type" else _whole(v) if k == "value"
                                           else _masked(v))
                                       for k, v in fld.items()} for fld in f["fields"]]}
        if t.get("is_submitter"):
            tgt["submits_form"] = {"method": t["submit_method"],
                                   "destination": destination(t["submit_action"], page_url)}
        if action.get("then_enter") and t.get("enter_action"):
            tgt["enter_submits_form"] = {"method": t["enter_method"],
                                         "destination": destination(t["enter_action"], page_url)}
        if t.get("is_submitter") and t.get("submission"):
            tgt["submission"] = shown_submission(t["submission"], page_url)
        if action.get("then_enter") and t.get("enter_submission"):
            tgt["enter_submission"] = shown_submission(t["enter_submission"], page_url)
        tgt["nearest_heading"] = _mt(t["heading"]) if t.get("heading") else None
        pkt["target"] = tgt
    else:
        pkt["target"] = {"role": "page"}
    # Every field's value is shown whole exactly once: in ``target.form.fields`` when the target's
    # form lists it, else in its control line (a field with no form owner, or another form's).
    pkt["page"] = {"blocks": out, "page_truncated": truncated,
                   "controls": [control_line(c, whole_value=not in_target_form(c, target))
                                for c in controls]}
    pkt["run_so_far"] = list(run_so_far)
    if download:
        pkt["downloaded_file"] = download
    if plan is not None:
        pkt["plan"] = plan
    if approved is not None:
        pkt["approved"] = approved
    return pkt


def go_allowed(category: str, packet: dict) -> bool:
    """F2: reading and local steps, and an account change only under what he approved, with no
    Enter after typing (nothing re-reads the whole page between the typing and the Enter)."""
    return category in GO_CATEGORIES or (category == "account_change" and "approved" in packet
                                         and not packet["action"].get("then_enter"))


MAX_SHOWN_ENTRIES = 100


def shown_submission(sub: dict, page_url: str, *, cut: bool = True) -> dict:
    """A submission as the check is shown it: method, destination (origin and masked path),
    enctype, target, the submitter's role and masked name, and every entry masked (a secret
    one already ``[not shown]``); in a packet values are cut to 200 characters and at most 100
    entries are listed, in a difference nothing is cut."""
    if sub.get("none") or sub.get("unreadable"):
        return {k: v for k, v in sub.items() if k in ("none", "unreadable")}
    show = _shown if cut else (lambda v: v if v == NOT_SHOWN else _mf(v))
    entries = [[(_mt(k) if cut else _mf(k))[:100] if cut else _mf(k),
                show(v)[:200] if cut else show(v)] for k, v in sub.get("entries") or ()]
    out = {"method": sub.get("method"), "destination": destination(sub.get("action"), page_url),
           "enctype": sub.get("enctype"), "entries": entries[:MAX_SHOWN_ENTRIES] if cut else entries}
    if cut and len(entries) > MAX_SHOWN_ENTRIES:
        out["entries_truncated"] = True
    if sub.get("target"):                  # a name the page chose: masked like any page text
        out["target"] = _mt(sub["target"]) if cut else _mf(sub["target"])
    s = sub.get("submitter")
    if s:
        out["submitter"] = {"role": s.get("role"), "name": (_mt(s.get("name")) if cut else _mf(s.get("name")))[:300]}
    return out


def canonical(packet: dict) -> bytes:
    """The packet's canonical bytes (plan §1): sorted keys, no ASCII escaping, compact."""
    return json.dumps(packet, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- target facts (F4)

def exact_destination(t: dict | None, *, enter: bool = False) -> str | None:
    """A control's exact destination, query included (app-private): a link's address, a
    submitter's effective submission destination, a text field's Enter destination."""
    if not t:
        return None
    if enter:
        return json.dumps([t.get("enter_action"), t.get("enter_method"), t.get("enter_target")])
    if t.get("href"):
        return t["href"]
    if t.get("activator_href"):        # a press inside a link runs the link
        return t["activator_href"]
    if t.get("is_submitter"):
        return json.dumps([t.get("submit_action"), t.get("submit_method"), t.get("submit_target")])
    return None


def form_identity(t: dict) -> Any:
    f = t.get("form")
    if not f:
        return None
    return [f.get("method"), f.get("action"), f.get("target"),
            [[x.get("label"), x.get("type")] for x in f.get("fields") or ()]]


def target_facts(t: dict | None, *, enter: bool = False) -> dict | None:
    """The target's own facts a go freezes and the last moment re-reads (design F4): role,
    accessible name, tag and input type, form identity and field values, its state, and its
    exact destination (for a ``type`` that ends in Enter, also the Enter destination)."""
    if t is None:
        return None
    own = t.get("form_self", -1)
    return {"role": t.get("role"), "name": t.get("name"), "tag": t.get("tag"),
            "input_type": t.get("input_type"), "state": t.get("state"),
            "form": form_identity(t),
            "form_fields": [[x.get("value"), x.get("checked"), x.get("selected")]
                            for i, x in enumerate((t.get("form") or {}).get("fields") or ())
                            if i != own],
            "destination": exact_destination(t),
            "enter_destination": exact_destination(t, enter=True) if enter else None,
            # A: what pressing it, or its Enter, would submit, as the browser builds it
            "submission": t.get("submission"),
            "enter_submission": t.get("enter_submission") if enter else None}


def facts_but_state(facts: dict | None) -> dict | None:
    """A text field's facts while it is typed into: everything but its own state (its value,
    an autocomplete's ``aria-expanded``), which the typing changes."""
    return None if facts is None else {k: v for k, v in facts.items() if k != "state"}


def resolve_matches(reading: dict, frozen: dict, frame_href: str | None = None) -> list[int]:
    """Design §2.6: the controls of a fresh whole-page reading with the frozen role, name, form
    identity and exact destination, in a frame at the target's frame address."""
    out = []
    for c in reading.get("controls") or ():
        if frame_href is not None and c.get("frame_href") != frame_href:
            continue
        if (c.get("role") == frozen.get("role") and c.get("name") == frozen.get("name")
                and form_identity(c) == frozen.get("form")
                and exact_destination(c) == frozen.get("destination")):
            out.append(c["idx"])
    return out


# --------------------------------------------------------------------------- tap time (§2.3, F4)

def _projection(reading: dict) -> tuple:
    blocks = tuple(b["text"] for b in reading.get("blocks") or ())
    controls = tuple(json.dumps([c.get("role"), c.get("name"), c.get("tag"), c.get("input_type"),
                                 c.get("state"), exact_destination(c),
                                 exact_destination(c, enter=True) if c.get("enter_action") else None],
                                sort_keys=True, ensure_ascii=False)
                     for c in reading.get("controls") or ())
    return blocks, controls


def _same_view(a: dict, b: dict) -> bool:
    return _projection(a) == _projection(b) and _same_beside_the_page(a, b)


def _same_beside_the_page(a: dict, b: dict) -> bool:
    return (list(a.get("frames_withheld") or ()) == list(b.get("frames_withheld") or ())
            and _submissions(a) == _submissions(b))


def blind(reading: dict) -> bool:
    """The reading fell short for a cause other than the control cap (a limit on roots or blocks,
    an unreadable frame, a closed shadow root), so nothing can tell two of them apart. A reading
    that says it is short and is not capped counts as one."""
    return bool(reading.get("blind")) or not (reading.get("complete") or "all_controls" in reading)


def target_record(reading: dict) -> list[str]:
    """The text of the target's record (table row, list item, article): its blocks, in document
    order. Empty for a target outside any record. The ids belong to one reading, never compared."""
    record = (reading.get("target") or {}).get("record")
    if record is None:
        return []
    return [b["text"] for b in reading.get("blocks") or () if b.get("record") == record]


def same_reading(a: dict, b: dict) -> bool:
    """The final read (ruling 8, no exemption): neither reading blind, every block's text, the
    target's record, every withheld frame's content and what it would submit equal; and every
    control's state and exact destination equal: past the list's cap (``all_controls`` on either
    side) all of them per frame, otherwise the list itself."""
    if blind(a) or blind(b) or target_record(a) != target_record(b):
        return False
    if "all_controls" not in a and "all_controls" not in b:
        return _same_view(a, b)
    return (a.get("all_controls") == b.get("all_controls")
            and _projection(a)[0] == _projection(b)[0] and _same_beside_the_page(a, b))


def same_as_judged(a: dict, b: dict) -> bool:
    """A go under an approval: everything the judged packet was built from is unchanged, its
    title, address and card fields too; a capped reading is held to what it shows."""
    return all(a.get(k) == b.get(k) for k in ("title", "href", "has_cc")) and _same_view(a, b)


def _submissions(r: dict) -> tuple:
    t = r.get("target") or {}
    return t.get("submission"), t.get("enter_submission")


def _pairs(a: list, b: list):
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        yield op, a[i1:i2], b[j1:j2]


def _mf(s: str | None) -> str:
    """Masked, never cut: the difference shows a whole value (a large one makes the
    difference too large, which has no ``same``)."""
    return mask_text(s or "", limit=10 ** 9).text


def full_line(c: dict) -> str:
    """A control as the difference shows it: like ``control_line`` with nothing cut."""
    def shown(v):
        return v if v == NOT_SHOWN else _mf(v)
    st = {}
    for k, v in (c.get("state") or {}).items():
        if isinstance(v, str):
            st[k] = shown(v)
        elif isinstance(v, list):
            st[k] = [shown(x) if isinstance(x, str) else x for x in v]
        else:
            st[k] = v
    s = f'{c["role"]}: {_mf(c.get("name")).strip() or "(unnamed)"}'
    if st:
        s += " " + json.dumps(st, ensure_ascii=False)
    return s


def _changed(before: str, after: str) -> dict:
    out = {"before": before, "after": after}
    if before == after:
        out["masked_text_changed"] = True     # the raw texts differ where masking hides them
    return out


def difference(a: dict, b: dict) -> dict:
    """The difference between two whole readings (never the capped packets): blocks added,
    removed or changed, and controls whose role, name or state changed, matched by index and
    then by role and name; a change in the text of the target's record; past the list's cap, not
    the list's shifts but whether any control changed. Whether something changed is decided on the
    complete raw values; what the check is shown is masked and never cut."""
    A = [x["text"] for x in a.get("blocks") or ()]
    B = [x["text"] for x in b.get("blocks") or ()]
    out: dict[str, Any] = {"blocks_added": [], "blocks_removed": [], "blocks_changed": [],
                            "controls_changed": [], "controls_added": [], "controls_removed": []}
    for op, old, new in _pairs(A, B):
        if op == "insert":
            out["blocks_added"] += [_mf(t) for t in new]
        elif op == "delete":
            out["blocks_removed"] += [_mf(t) for t in old]
        elif op == "replace":
            for k in range(max(len(old), len(new))):
                o = old[k] if k < len(old) else None
                n = new[k] if k < len(new) else None
                if o is None:
                    out["blocks_added"].append(_mf(n))
                elif n is None:
                    out["blocks_removed"].append(_mf(o))
                else:
                    out["blocks_changed"].append(_changed(_mf(o), _mf(n)))
    ca = list(a.get("controls") or ())
    cb = list(b.get("controls") or ())
    used: set[int] = set()
    key = lambda c: (c.get("role"), c.get("name"))  # noqa: E731
    for i, c in enumerate(ca):
        j = None
        if i < len(cb) and i not in used and key(cb[i]) == key(c):
            j = i
        else:
            j = next((k for k, d in enumerate(cb) if k not in used and key(d) == key(c)), None)
        if j is None:
            out["controls_removed"].append(full_line(c))
            continue
        used.add(j)
        raw = lambda d: json.dumps([d.get("role"), d.get("name"), d.get("state")],  # noqa: E731
                                   sort_keys=True, ensure_ascii=False)
        if raw(c) != raw(cb[j]):
            out["controls_changed"].append(_changed(full_line(c), full_line(cb[j])))
    out["controls_added"] += [full_line(d) for k, d in enumerate(cb) if k not in used]
    sa, sb = _submissions(a), _submissions(b)
    if sa != sb:
        # A: what the press or the Enter would submit changed (an entry, the submitter, the
        # destination): shown whole, masked.
        url = a.get("href") or ""
        show = lambda x: None if x is None else shown_submission(x, url, cut=False)  # noqa: E731
        before, after = [show(x) for x in sa], [show(x) for x in sb]
        out["submission_changed"] = {"before": before, "after": after}
        if before == after:
            out["submission_changed"]["masked_text_changed"] = True   # only a value not shown
    fa, fb = list(a.get("frames_withheld") or ()), list(b.get("frames_withheld") or ())
    if fa != fb:
        out["frames_not_shown_changed"] = [n + 1 for n in range(max(len(fa), len(fb)))
                                           if fa[n:n + 1] != fb[n:n + 1]]
    ra, rb = target_record(a), target_record(b)
    if ra != rb:
        out["target_record_changed"] = _changed([_mf(t) for t in ra], [_mf(t) for t in rb])
    if "all_controls" in a or "all_controls" in b:
        # Past the cap the list shifts with the viewport, which is no change: only the digests say
        # whether a control changed, and code decides nothing on it beyond telling the check.
        out["controls_added"], out["controls_removed"] = [], []
        if a.get("all_controls") == b.get("all_controls"):
            out["controls_changed"] = []
        else:
            out["controls_beyond_list_changed"] = True
    return {k: v for k, v in out.items() if v}


def difference_fits(diff: dict) -> bool:
    return len(json.dumps(diff, ensure_ascii=False)) <= MAX_DIFFERENCE


# --------------------------------------------------------------------------- the check's reply

def _str_within(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _labels_within(value: Any, count: int, limit: int) -> bool:
    return (isinstance(value, list) and len(value) <= count
            and all(_str_within(v, limit) for v in value))


def verdict_within_bounds(kind: str, v: Any) -> bool:
    """The app's own copy of the check's reply bounds (S2-7 step 3): a verdict outside them is
    no verdict. ``kind`` is ``action`` or ``rejudge``."""
    if not isinstance(v, dict) or v.get("verdict") not in VERDICTS.get(kind, ()):
        return False
    if not _str_within(v.get("reason", ""), MAX_REASON_CHARS):
        return False
    if kind == "rejudge":
        return isinstance(v.get("text_incomplete"), bool)
    if v.get("category") not in CATEGORIES:
        return False
    if not isinstance(v.get("look_first", ""), str):
        return False
    if v["verdict"] != "ask":
        return True
    q = v.get("question")
    return (isinstance(q, dict) and isinstance(q.get("text_incomplete"), bool)
            and _str_within(q.get("text"), MAX_TEXT_CHARS)
            and _str_within(q.get("confirm_label"), MAX_LABEL_CHARS)
            and _labels_within(q.get("extra_options", []), MAX_EXTRA_OPTIONS, MAX_OPTION_CHARS)
            and q.get("screenshot") in SCREENSHOTS
            and _labels_within(q.get("highlight", []), MAX_HIGHLIGHTS, MAX_HIGHLIGHT_CHARS))


# --------------------------------------------------------------------------- logins (slice 5)

MAX_LOGIN_ITEMS = 5              # a login_item reply: at most five candidates (S5-2)
SIGNIN_READING_CAP = 3_000       # characters of block text in one sign-in reading (S5-5)
SIGNIN_VERDICTS = ("signed_in", "not_signed_in", "unsure")


def _index(v: Any) -> bool:
    return type(v) is int and v >= 0


def login_item_within_bounds(v: Any, n_items: int) -> bool:
    """A ``login_item`` reply: at most five distinct indexes of the packet's own items."""
    items = v.get("items") if isinstance(v, dict) else None
    return (isinstance(items, list) and len(items) <= MAX_LOGIN_ITEMS
            and all(_index(i) and i < n_items for i in items) and len(set(items)) == len(items)
            and _str_within(v.get("reason"), MAX_REASON_CHARS))


def signin_within_bounds(v: Any, blocks: set) -> bool:
    """A ``signin`` reply: its verdict, and evidence that names the packet's own blocks."""
    ev = v.get("evidence_blocks") if isinstance(v, dict) else None
    return (v.get("verdict") in SIGNIN_VERDICTS and isinstance(ev, list) and len(ev) <= 50
            and all(type(n) is int and n in blocks for n in ev)
            and _str_within(v.get("reason"), MAX_REASON_CHARS))


def signin_reading(data: dict) -> dict:
    """One page as the sign-in check sees it (design §5.3 step 3): title, host, masked path,
    masked text blocks (capped), the actionable elements as role and name only — no value or
    state of any field (d1 Astra B; the reader's ``valueOf`` already hides secret fields'
    values) — and whether a password field is shown."""
    href = data.get("href") or ""
    blocks, used = [], 0
    for b in data.get("blocks") or ():
        text = _mt(b.get("text"))
        if used + len(text) > SIGNIN_READING_CAP:
            break
        blocks.append(text)
        used += len(text)
    controls = data.get("controls") or ()
    return {"title": _mt(data.get("title")), "host": urlsplit(href).hostname,
            "path": masked_path(href), "blocks": blocks,
            "controls": [f'{c.get("role")}: {_mt(c.get("name"))[:200]}' for c in controls],
            "password_field": any(c.get("input_type") == "password" for c in controls)}


async def read_signin_page(ops, page, policy) -> dict | None:
    """``signin_reading`` of ``page`` by the gate's own reader (its secret choke point, its
    masking, the page-view gate), with the page's origin under the private key ``_origin``;
    ``{"not_shown": true}`` for a page that is not this session's; None when it cannot be
    read."""
    try:
        reading = await ops.read_page(page, None, policy=policy)
    except LookupError:
        return {"not_shown": True}
    except Exception:  # noqa: BLE001 — a page that cannot be read is not shown
        return None
    out = signin_reading(reading.data)
    parts = urlsplit(reading.data.get("href") or "")
    default = {"http": 80, "https": 443}.get(parts.scheme)
    try:
        port = parts.port
    except ValueError:
        port = None
    out["_origin"] = (f"{parts.scheme}://{(parts.hostname or '').lower()}"
                      + (f":{port}" if port and port != default else "")) if default else None
    return out


def signin_packet(site: str, origins: list, after: list, at_done: dict) -> dict:
    """The ``signin`` packet: the pages after the last attempt and the page at Done, blocks
    numbered across the packet (the check names its evidence by these numbers)."""
    n = 0

    def numbered(r):
        nonlocal n
        if r.get("not_shown"):
            return {"not_shown": True}
        out = dict(r, blocks=[])
        for text in r["blocks"]:
            n += 1
            out["blocks"].append({"n": n, "text": text})
        return out
    return {"kind": "signin", "site": {"name": site, "origins": list(origins)},
            "after_attempt": [numbered(r) for r in after], "at_done": numbered(at_done)}


SIGN_IN_VERDICTS = ("sign_in", "not_sign_in")


def sign_in_within_bounds(v: Any) -> bool:
    """A ``sign_in`` reply (signing in off the start page): its verdict, a reason and (R9)
    ``fields``: the planned field id the username and the password go into, each optional."""
    if not (isinstance(v, dict) and v.get("verdict") in SIGN_IN_VERDICTS
            and _str_within(v.get("reason"), MAX_REASON_CHARS)):
        return False
    fields = v.get("fields", {})
    return (isinstance(fields, dict) and set(fields) <= {"username", "password"}
            and all(f is None or _str_within(f, 16) for f in fields.values()))


def sign_in_packet(reading: dict, *, site: str, origins: list, planned: dict) -> dict:
    """The ``sign_in`` packet: the site, the page as an action packet reads it (title, path,
    numbered masked blocks, its controls) and what a sign-in there would do (``planned``: the
    fields by role and masked label, the button's masked label, the passkey)."""
    base = build_packet(reading, None, {"kind": "sign_in"}, task="", session="signed_in",
                        site=site)
    where = base["where"]
    return {"kind": "sign_in",
            "site": {"name": site, "registered_origins": list(origins), "host": where["host"]},
            "page": {"title": where["title"], "path": where["path"], **base["page"]},
            "planned": planned}


def packet_blocks(packet: dict) -> set:
    return {b["n"] for r in (*packet["after_attempt"], packet["at_done"])
            for b in r.get("blocks") or ()}


def one_line(text: Any, limit: int = MAX_REASON_SHOWN) -> str:
    """A masked one-line rendering of the check's text (its reason, a note)."""
    s = mask_text(text if isinstance(text, str) else "").text
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- pending judgements

@dataclass(eq=False)
class PendingJudgement:
    """One action frozen for its verdict (S2-5). Memory only."""

    id: str
    kind: str                       # act | download | search | rejudge
    packet_bytes: bytes = field(repr=False)
    digest: str
    generation: int
    session_ref: Any
    url: str = field(repr=False)    # the page address at freeze, fragment included
    created_at: Any                 # the controller clock's datetime
    action: Any = None              # protocol.Action (act), or None
    target: Any = field(default=None, repr=False)   # handles.PrivateTarget
    frozen: dict | None = field(default=None, repr=False)
    reading: dict | None = field(default=None, repr=False)   # the whole reading judged
    packet: dict | None = field(default=None, repr=False)
    label: str = ""                 # "<kind> “<masked name>”" for run_so_far
    verb: str = ""                  # the action kind the packet names
    dest_kind: str = "account"      # the session the input is delivered in
    handle: str | None = None       # a download's handle
    search_url: str | None = field(default=None, repr=False)
    enter: bool = False
    choice: Any = field(default=None, repr=False)    # a select's option target
    option: dict | None = None      # the option's frozen label and value
    via: str | None = None          # a download: "link" or "button"
    quarantine: Any = field(default=None, repr=False)   # a public download's fetched file (S4-6)
    nav: bool = False               # dispatched as a navigation (``_goto``)
    entry_name: str = ""            # an app-issued link's label
    typed_judged: bool = False      # a tapped type+Enter whose typed page was re-judged
    destination_changed: bool = False   # a re-judgement's private destination fact
    after_lapse: bool = False        # judged afresh because his Yes lapsed: its card says so
    frame_url: str | None = field(default=None, repr=False)   # the target's frame address
    # The page's picture for the check: memory only, gone once the verdict starts applying.
    picture: bytes | None = field(default=None, repr=False)
    # A re-judgement (kind ``rejudge``): the question it serves, how many re-judgements this
    # tap has run, and whether it judges the typed page of a tapped type+Enter.
    question_id: str | None = None
    rejudges: int = 0
    post_typing: bool = False
    state: str = "open"             # open | applying
    dispatch_started: bool = False

    @property
    def packet_text(self) -> str:
        return self.packet_bytes.decode("utf-8")

    def wire(self) -> dict:
        return {"id": self.id, "kind": "rejudge" if self.kind == "rejudge" else "action",
                "packet": self.packet_text}


# --------------------------------------------------------------------------- page operations

# F9's hit test asks the browser: CDP ``DOM.getNodeForLocation`` reports the node that
# receives input at a point of a frame's document; the node must be the target (or, at an outer
# level, the frame element holding the next out-of-process frame) or inside it. Identity is node
# identity, never a mark on the page: a custom event of a random type is dispatched on the
# target from Patchright's isolated world, a listener in a fresh isolated world of the reported
# node's frame keeps the node it was dispatched on, and that world compares it with the reported
# node's composed ancestors. Nothing on the page changes, and no main-world function is used.
# The listener keeps the node in a closure object (never a window property, which a page element
# named like it would answer); the walk follows the flat tree: an element's assigned slot first,
# else its parent, through shadow roots to their hosts; a pseudo-element starts at its element.
_KEEP = """(() => { const box = {t: null};
  addEventListener(%s, (e) => { box.t = e.composedPath()[0]; }, {capture: true, once: true});
  return box; })()"""
_INSIDE = """function (box) {
  const t = box && box.t;
  if (!t) return false;
  let c = this.nodeType ? this : this.element;
  for (; c; c = c.assignedSlot || (c.nodeType === 11 ? c.host
         : ((c.parentNode && c.parentNode.nodeType === 11) ? c.parentNode.host : c.parentNode))) {
    if (c === t) return true;
    // the press would run this descendant's activation (a link or button inside the target)
    if (c.nodeType === 1 && c.matches('a[href],area[href],button,input:not([type=hidden]),label,summary')) return false;
  }
  return false;
}"""
_SEPARATE = "does not have a separate CDP session"


# The deep active element (through open shadow roots' activeElement) is the target, and its
# document has the focus.
_FOCUSED = r"""(e) => {
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  return a === e && document.hasFocus();
}"""

# S2-4 rule 0: the facts that decide whether a ``type`` may target this element. Slice 5g: a field
# of ``VALUE_TYPES`` (``value_kind``) takes its value as one in-page step; ``accepts`` says whether
# the browser's own value format takes the text, asked of a detached input of the same type with
# the field's min/max/step/value attributes (the page is not touched); ``readonly`` fields take no typing.
# Slice 6b: ``textarea`` — a real <textarea>, the only field a line break is typed into.
_TEXT_FACTS = r"""(e, text) => {
  /*SECRET*/
  const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
  const role = (e.getAttribute('role') || '').toLowerCase();
  const editableText = tag === 'input' ? ['', 'text', 'search', 'email', 'tel', 'url', 'number'].includes(type)
    : (tag === 'textarea' || e.isContentEditable);
  const valueKind = tag === 'input' && /*VALUE_TYPES*/.includes(type) ? type : null;
  let accepts = null;
  if (valueKind && typeof text === 'string') {
    const probe = document.createElement('input');
    probe.type = type;
    // the step base is min, else the value attribute (r1 Astra): both copied
    for (const a of ['min', 'max', 'step', 'value']) if (e.hasAttribute(a)) probe.setAttribute(a, e.getAttribute(a));
    probe.value = text;
    accepts = probe.value === text;
  }
  // A value never shown in a packet is never typed or chosen by the agent.
  return {editable_text: editableText, secret: hidesSecret(e), value_kind: valueKind, accepts,
          readonly: (tag === 'input' || tag === 'textarea') && e.readOnly,
          textarea: e.localName === 'textarea' && e.namespaceURI === 'http://www.w3.org/1999/xhtml',
          search_field: role === 'searchbox' || (tag === 'input' && type === 'search')};
}"""

# Slice 5g, the select's pattern (S2-8) for a value field: one in-page step in one JavaScript task
# (no page script runs before the events) that checks the field (attached, the frozen tag and
# type, visible, enabled, not read-only, not secret), sets the judged text and reads it back: when
# the browser's value format refuses it the old value is put back and nothing is fired
# (``not_taken``: nothing was dispatched). Otherwise it fires ``input`` and ``change`` as a user's
# change does and reads the value again after the page's own listeners ran: ``ok`` when the field
# still holds the judged text, ``changed_by_page`` when the page changed it (dispatched).
_SET_VALUE = r"""(e, [type, text]) => {
  /*SECRET*/
  if (!e || !e.isConnected || e.tagName !== 'INPUT' || (e.getAttribute('type') || '').toLowerCase() !== type) return 'field';
  if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) || e.matches(':disabled') || e.readOnly || hidesSecret(e)) return 'field';
  const before = e.value;
  e.value = text;
  if (e.value !== text) { e.value = before; return 'not_taken'; }
  e.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
  return e.value === text ? 'ok' : 'changed_by_page';
}"""

_VALUE = r"""(e) => { /*SECRET*/
  if (hidesSecret(e)) return NOT_SHOWN;
  return (e.isContentEditable && !('value' in e)) ? e.innerText : (e.value === undefined ? null : String(e.value)); }"""

# The chosen option's facts, and whether it belongs to the select.
_OPTION_FACTS = r"""(s, o) => ({
  belongs: !!o && o.tagName === 'OPTION' && !!s.options && [...s.options].includes(o),
  label: o ? o.label.replace(/\s+/g, ' ').trim() : null,     // the name the option shows
  value: o ? o.value : null})"""

# The option of ``s`` whose visible name is the same as ``o``'s, when exactly one has it (none,
# several, or not a select and an option: null). An observation cut short lists a long form's
# dropdown only in part, and the same name listed under another row is how the agent reaches it.
_OWN_OPTION = r"""(s, o) => {
  if (!s || s.tagName !== 'SELECT' || !o || o.tagName !== 'OPTION' || !s.options) return null;
  const name = (x) => x.label.replace(/\s+/g, ' ').trim();     // the name a dropdown shows
  const same = [...s.options].filter((x) => name(x) === name(o));
  return same.length === 1 ? same[0] : null;
}"""

# S2-8: a selection is one in-page step. In one JavaScript task (no page script can run in
# between) it checks the select (attached, visible, enabled) and the option (attached, in that
# select, not disabled itself or through a disabled optgroup, the frozen label and value), and
# only then selects it and dispatches input and change, as a user's selection does. It returns
# which check failed, or ok. Nothing waits and nothing retries.
_SELECT_ONE = r"""(s, [o, label, value]) => {
  if (!s || !s.isConnected || s.tagName !== 'SELECT') return 'select';
  if (!s.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) || s.matches(':disabled')) return 'select';
  if (!o || !o.isConnected || !s.options || ![...s.options].includes(o)) return 'option';
  if (o.disabled || (o.parentElement && o.parentElement.tagName === 'OPTGROUP' && o.parentElement.disabled)) return 'option_disabled';
  if (o.label.replace(/\s+/g, ' ').trim() !== label || o.value !== value) return 'option_changed';
  o.selected = true;
  s.dispatchEvent(new Event('input', {bubbles: true}));
  s.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok';
}"""


async def _inside(session, found: dict, handle, nonce: str) -> bool:
    """Whether the reported node is ``handle``'s element or inside it, compared as nodes in a
    fresh isolated world of the reported node's frame (an element in another frame never is)."""
    world = await session.send("Page.createIsolatedWorld", {
        "frameId": found["frameId"], "worldName": "casa-hit-" + nonce[:8]})
    ctx = world["executionContextId"]
    kind = "casa-hit-" + nonce
    box = await session.send("Runtime.evaluate", {"expression": _KEEP % json.dumps(kind),
                                                  "contextId": ctx})
    await handle.evaluate("(e, k) => e.dispatchEvent(new CustomEvent(k, {bubbles: true, composed: true}))",
                          kind)
    node = await session.send("DOM.resolveNode", {"backendNodeId": found["backendNodeId"],
                                                  "executionContextId": ctx})
    got = await session.send("Runtime.callFunctionOn", {
        "objectId": node["object"]["objectId"], "functionDeclaration": _INSIDE,
        "arguments": [{"objectId": box["result"]["objectId"]}], "returnByValue": True})
    return got.get("result", {}).get("value") is True


async def _origin(session):
    """The document point of the session's viewport origin (the visual viewport's signed page
    offset: a right-to-left page scrolls to negative x) and the layout viewport's size; None when
    the visual viewport is zoomed or offset within the layout one (a pinch zoom: not mapped)."""
    m = await session.send("Page.getLayoutMetrics")
    lay, vis = m["cssLayoutViewport"], m.get("cssVisualViewport")
    if not vis or abs(vis.get("scale", 1) - 1) > 1e-9 or abs(vis.get("offsetX", 0)) > 1e-6 \
            or abs(vis.get("offsetY", 0)) > 1e-6:
        return None
    return vis["pageX"], vis["pageY"], lay["clientWidth"], lay["clientHeight"]


def _corners(x: float, y: float) -> list:
    """The integer points around a document point (one when it is integral)."""
    import math
    return sorted({(math.floor(x), math.floor(y)), (math.ceil(x), math.floor(y)),
                   (math.floor(x), math.ceil(y)), (math.ceil(x), math.ceil(y))})


async def _into_frame(session, holder: int, viewport: tuple, point):
    """A viewport point of a frame, as a viewport point inside the out-of-process frame its
    holder shows: the inverse of the affine map from the child's viewport (``viewport``: its
    width and height) to the holder's content quad as the browser laid it out, transforms
    included. None for a quad that is not a parallelogram (3D) or is degenerate, or a point
    outside it."""
    model = await session.send("DOM.getBoxModel", {"backendNodeId": holder})
    x0, y0, x1, y1, x2, y2, x3, y3 = model["model"]["content"]
    vw, vh = viewport
    ax, ay, bx, by = x1 - x0, y1 - y0, x3 - x0, y3 - y0
    if abs((x2 - x0) - (ax + bx)) > 0.5 or abs((y2 - y0) - (ay + by)) > 0.5:
        return None
    det = ax * by - ay * bx
    if abs(det) < 1e-6 or not vw or not vh:
        return None
    px, py = point[0] - x0, point[1] - y0
    u = (px * by - py * bx) / det
    v = (ax * py - ay * px) / det
    if not (0 <= u <= 1 and 0 <= v <= 1):
        return None
    return u * vw, v * vh


class PageOps:
    """The gate's fixed page operations (a controller collaborator; the tests' doubles stand in
    for it). Every script is fixed code in the isolated world; none takes model input."""

    async def read(self, frame, target=None, *, with_handles: bool = False) -> Reading:
        return await read(frame, target, with_handles=with_handles)

    async def read_page(self, page, target=None, *, policy=None,
                        with_handles: bool = False) -> Reading:
        """The whole page: the target's frame (the reading the packet is built from, its
        controls first so their indexes hold), then every other frame — shown under the one
        frame rule the observation shows frames by (``observe.frame_shown``, slice 5m), else as
        a placeholder whose content is compared by digest. Frames are read parents first.
        ``complete`` is false when any frame's reading is, or cannot be had, or a closed shadow
        root holds a field; all but the control cap also make it ``blind``."""
        frame, got, views = await self._read_frames(page, target, policy, with_handles)
        shadow = await self.closed_shadow(page)
        return self._assemble(frame, got, views, with_handles, shadow)

    async def read_page_and_bound(self, page, frames, *, policy=None) -> tuple:
        """One pass over the page's frames (signing in off the start page): the whole page
        reading, and from the very same frame readings the reading of the main frame and
        ``frames`` only (a look's bound documents; every other frame is left out of it)."""
        frame, got, views = await self._read_frames(page, None, policy, False)
        shadow = await self.closed_shadow(page)
        main = page.main_frame
        bound = [(f, r) for f, r in got if f is main or any(f is b for b in frames)]
        return (self._assemble(frame, got, views, False, shadow),
                self._assemble(frame, bound, views, False, shadow))

    async def _read_frames(self, page, target, policy, with_handles):
        from casa_browser.observe import child_frames, frame_shown, page_view_allowed
        frame = await self.frame_of(page, target)
        main = page.main_frame
        # The page read is the session's own (design §4), asked of the address before the read
        # and of the address the reading itself reports.
        if not page_view_allowed(page.url, policy):
            raise LookupError("not_this_sessions_page")
        got: list = []          # (frame, its reading, or None when it could not be read)
        views: dict = {}        # id(frame) -> (shown, origin)
        for f in [main, *child_frames(page)]:
            address = f.url
            try:
                r = await read(f, target if f is frame else None, with_handles=with_handles)
            except Exception:  # noqa: BLE001 — a frame that cannot be read is not compared
                if f is frame:
                    raise
                got.append((f, None))
                views[id(f)] = (False, None)
                continue
            href, origin = r.data.get("href"), r.data.get("origin")
            if f is main:
                # the page's own frame: its address admitted before and after the read
                ok = page_view_allowed(address, policy) and page_view_allowed(href, policy)
                if f is not frame:
                    # as another frame (r3 Astra 1a-y): the address it had before the read
                    ok = ok and href == address
            else:
                ok = frame_shown(address, href, origin, views.get(id(f.parent_frame)), policy)
            got.append((f, r))
            views[id(f)] = (ok, origin)
        if not views.get(id(frame), (False,))[0]:
            raise LookupError("not_this_sessions_page")
        return frame, got, views

    @staticmethod
    def _assemble(frame, got, views, with_handles, shadow) -> Reading:
        primary = next(r for f, r in got if f is frame)
        data = dict(primary.data, blocks=list(primary.data["blocks"]),
                    controls=[dict(c, frame_href=primary.data["href"])
                              for c in primary.data["controls"]])
        if any(r is None for _f, r in got):
            data["complete"] = False
            data["blind"] = True
        handles = list(primary.handles or ()) if with_handles else None
        k = 0
        for f, r in got:
            if f is frame:
                continue
            k += 1
            if r is None:
                continue
            shown = views[id(f)][0]
            merge_frame(data, r.data, k, shown=shown)
            if with_handles and shown:
                handles += list(r.handles or ())
        if shadow:
            data["complete"] = False
            data["blind"] = True
        if any(r is not None and r.data.get("all_controls") for _f, r in got):
            # One value per shown frame, in frame order: a change in a small frame beside a long
            # one counts. A withheld frame is compared under ``frames_withheld``.
            data["all_controls"] = [r.data.get("all_controls") or listed_digest(r.data)
                                    for f, r in got if r is not None and views[id(f)][0]]
        return Reading(data, handles)

    async def closed_shadow(self, page) -> bool:
        from casa_browser.observe import closed_shadow_holds_field
        return await closed_shadow_holds_field(page)

    async def frame_of(self, page, element):
        if element is None:
            return page.main_frame
        frame = await element.owner_frame()
        return frame or page.main_frame

    async def hit(self, element, point) -> bool:
        """F9: the browser's own answer to "which element receives input at this (integer)
        viewport point" is the target or inside it — level by level through out-of-process
        frames, each entered by the browser's own box model of its holder. Any failure: no."""
        try:
            return await self._hit(element, (int(point[0]), int(point[1])))
        except Exception:  # noqa: BLE001 — a check that cannot be made does not pass
            return False

    async def _hit(self, element, point) -> bool:
        frame = await element.owner_frame()
        if frame is None:
            return False
        page = frame.page
        context = page.context
        chain = []
        f = frame
        while f is not None:
            chain.append(f)
            f = f.parent_frame
        chain.reverse()                                    # the page's frame ... the target's
        sessions = [await context.new_cdp_session(page)]
        holders = []
        try:
            for f in chain[1:]:
                try:
                    own = await context.new_cdp_session(f)
                except Exception as err:  # noqa: BLE001
                    if _SEPARATE in str(err):
                        continue                           # same process: the level above sees it
                    raise
                sessions.append(own)
                holders.append(await f.frame_element())
            nonce = secrets.token_hex(12)
            marks = [*holders, element]                    # what each level's node must be inside
            views = [point]                                # viewport points at this level
            for level, session in enumerate(sessions):
                origin = await _origin(session)
                if origin is None:
                    return False
                ox, oy, _vw, _vh = origin
                following = []
                for vx, vy in views:
                    for x, y in _corners(vx + ox, vy + oy):
                        try:
                            found = await session.send("DOM.getNodeForLocation", {
                                "x": x, "y": y, "includeUserAgentShadowDOM": True,
                                "ignorePointerEventsNone": False})
                        except Exception:  # noqa: BLE001 — no node at the point
                            return False
                        if not await _inside(session, found, marks[level], nonce):
                            return False
                    if level + 1 < len(sessions):
                        child = await _origin(sessions[level + 1])
                        if child is None:
                            return False
                        mapped = await _into_frame(session, found["backendNodeId"],
                                                   (child[2], child[3]), (vx, vy))
                        if mapped is None:
                            return False
                        following.append(mapped)
                views = following
            return True
        finally:
            for session in sessions:
                try:
                    await session.detach()
                except Exception:  # noqa: BLE001
                    pass

    async def link_href(self, element):
        return await element.evaluate("(e) => e.tagName === 'A' ? e.href : null")

    async def focused(self, element) -> bool:
        return bool(await element.evaluate(_FOCUSED))

    async def text_facts(self, element, text: str | None = None) -> dict:
        return await element.evaluate(_TEXT_FACTS, text)

    async def set_value(self, element, input_type: str, text: str) -> str:
        return await element.evaluate(_SET_VALUE, [input_type, text])

    async def value(self, element):
        return await element.evaluate(_VALUE)

    async def option_facts(self, select, option) -> dict:
        return await select.evaluate(_OPTION_FACTS, option)

    async def own_option(self, select, option):
        """``select``'s own option of ``option``'s visible name, or None (``_OWN_OPTION``)."""
        handle = await select.evaluate_handle(_OWN_OPTION, option)
        own = handle.as_element()
        if own is None:
            await handle.dispose()
        return own

    async def select_one(self, select, option, label: str, value: str) -> str:
        return await select.evaluate(_SELECT_ONE, [option, label, value])


READ = READ.replace("/*SECRET*/", SECRET_JS).replace("/*CONTROLS*/", CONTROLS_JS).replace(
    "/*COMPARED*/", json.dumps(list(COMPARED)))
_TEXT_FACTS = _TEXT_FACTS.replace("/*SECRET*/", SECRET_JS).replace(
    "/*VALUE_TYPES*/", json.dumps(sorted(VALUE_TYPES)))
_SET_VALUE = _SET_VALUE.replace("/*SECRET*/", SECRET_JS)
_VALUE = _VALUE.replace("/*SECRET*/", SECRET_JS)
