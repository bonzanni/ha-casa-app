"""Observations and operator screenshots, behind the page-view gate (Plan 1 Task 7).

``Observer.observe`` returns what a model may see of the current page: its visible text and
its visible, actionable elements as opaque handles with masked labels — never an address or a
selector, and an attribute only as the name of an element with no visible one
(``gate.CONTROLS_JS``'s ``pageName``). An ordinary field also shows what it holds (slice 5j, operator ruling
2026-10-31: its value masked and bounded, a select's chosen option, a checkbox's or radio's
state) under the screenshots' one rule, as the field is when it is read: a password,
one-time-code or payment-card field (``gate.SECRET_JS``'s ``hidesSecret``) shows none, and every
field whose value is shown is asked that rule again right after the reading. A page that later
changes its own field (its type, its autocomplete, its place) shows what it then holds: the
residual of ruling 2026-10-20, a page can show its own value as text.
``Observer.capture_operator`` (the operator's own
screenshot) and ``Observer.capture_question`` (a question's picture, its target outlined) store
a screenshot for the operator; ``Observer.capture_for_model`` (the check's picture, Fido's
``run_look``) returns the same picture for a model and keeps it nowhere. All follow one rule
(``Observer._capture_png``, slice 5h): password, one-time-code and payment-card fields and every
child frame are blacked out; every other field shows what it holds, and text the page itself
renders stays visible, secret-looking or not (operator rulings 2026-10-20, 2026-10-29 and
2026-11-06: simple redaction of recognised secret fields). All:

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

from casa_browser.gate import (CONTROLS_JS, FIELD_KINDS, INPUT_KINDS, MAX_JUDGE_PICTURE_BYTES,
                               MULTI_LINE, SECRET_JS)
from casa_browser.handles import HandleRegistry, PrivateTarget
from casa_browser.masking import MASK, MAX_INPUT, MAX_TEXT, mask_text, mask_whole
from casa_browser.policy import RequestMeta, RunPolicy, decide
from casa_browser.protocol import ElementView, Observation

VIEW_REASONS = frozenset({"site_document", "public_session"})
MAX_ELEMENTS = 400
MAX_BLOCKS = 1500
MAX_VALUE = 500          # characters of a field's value read (slice 5j); a longer one ends in " …"
MAX_VALUE_SHOWN = 600    # after masking, which may lengthen it
FIND_NEAR = 2            # a search's neighbours either side, as the plugin lists them: an unnamed button
FRAME_WITHHELD = "[a frame on this page is not shown]"
CONTROLS_LEFT = ("[{count} more controls on this page are not listed: what is in view comes first. "
                 "An observation with find reaches any of them by its words.]")
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


def holds_words(words: str, *texts: str | None) -> bool:
    """Whether what a control shows (masked, as the observation shows it) holds these plain
    words, in any case."""
    needle = words.casefold()
    return any(needle in t.casefold() for t in texts if t)


def _masked_name(name) -> str:
    """A control's or an option's name as an observation shows it, before its length is cut."""
    return mask_text(name or "").text.strip()


def _masked_value(value: str) -> str:
    """What a field holds as an observation shows it."""
    return mask_text(value).text[:MAX_VALUE_SHOWN]


def search_ask(items: list, words: str) -> dict:
    """The one match of a search, on the masked text a reply would show, over what the page's first
    reading returned (``EXTRACT``'s ``searchable``): what the second reading is to put first, by
    ordinal only. The words never enter the page, so what masking hides can change nothing that
    follows from here: what is found, what is left out, the list and the order of its options."""
    found, options, near = [], [], set()
    for i, item in enumerate(items):
        value = item.get("value")
        own = holds_words(words, _masked_name(item.get("name")),
                          _masked_value(value) if isinstance(value, str) else None)
        held = [n for n, label in enumerate(item.get("options") or ())
                if holds_words(words, _masked_name(label))]
        if own:
            found.append(i)
        if held:
            options.append([i, held])
        if own or held:
            near.update(range(max(i - FIND_NEAR, 0), min(i + FIND_NEAR + 1, len(items))))
    return {"found": found, "options": options, "near": sorted(near)}


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
#   text (its own, its <label>, aria-labelledby targets), else what the page calls an element with
#   none (``pageName``: never a field's value, nothing on or holding a secret);
#   what an ordinary field holds (slice 5j: ``holds``), never a secret field's.
# * Mask inventory (uncapped): every form field and editable element with a box (a screenshot
#   masks the secret ones among them), every child frame. ``complete`` is false when a limit
#   was reached.
# * A search (an observation's ``find``) takes two evaluations, and its words enter neither.
#   ``ask.searchable`` returns, by ordinal among ALL the page's actionable elements, past the cap,
#   what a search may look at: each one's name and value as it would be listed (so never a secret
#   field's value) and a select's options (never a secret select's). ``search_ask`` matches on that,
#   masked. ``ask`` then holds only ordinals: what was found goes first in ``actionable``'s ranking
#   and its neighbours next, so they fit the cap and get handles wherever they are, each marked
#   ``found``; ``found_left`` counts those that did not fit. A page that changes between the two may
#   get a stale ranking: it is only a list, and a press goes through the same check. ``whole`` is
#   false when a limit left part of the page unread. ``READ`` passes no ranking.
MAX_ROWS = 20_000   # records one rows reading returns (``EXTRACT``'s rows mode)
EXTRACT = r"""(ask) => {
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
  const nameOf = (e) => { let name = '';
    const lb = e.getAttribute('aria-labelledby');
    if (lb) name = lb.split(/\s+/).map(i => { const t = document.getElementById(i);
      return t && box(t) && seen(t) && !editable(t) ? textOf(t) : ''; }).join(' ');
    if (!name && e.labels && e.labels.length) name = textOf(e.labels[0]);
    if (!name && !editable(e)) name = textOf(e);
    if (!name) name = pageName(e);
    return cut(name.trim(), 2000); };
  const optionName = (o) => cut(o.label.replace(/\s+/g, ' ').trim(), 2000);
  // Blocks: own text of every block holding visible, non-editable text (block -> its text).
  const readBlocks = () => { const blocks = new Map(), full = new Set();
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
    return blocks; };
  /*CONTROLS*/
  const els = [], data = [];
  const accepted = (e) => {
    const attrRole = (e.getAttribute('role') || '').toLowerCase();
    return !(attrRole && !ROLES.has(attrRole) && tagRole(e) === 'other'); };
  // ``run_read``'s rows: this same reading — its blocks, its field values and names, by the same
  // code — grouped by the record (a table row, a list item or an article) holding each one's
  // block, so a block's text stays whole for masking and a record shows nothing an observation
  // would not, and keeps what lies around a record nested in it. A record that renders no box of
  // its own (display: contents, inline) shares its block's record. The blocks go out unmasked
  // with their record and the elements the observation lists (``actionable``, no cap); masking is
  // done once over every frame's blocks together (``rows_text``), then shared out. What no record
  // holds comes after a frame's records, as one row marked ``outside``. ``complete`` is false past
  // MAX_ROWS records or a reading limit.
  if (ask && ask.rows) {
    const RECORD = 'tr,[role=row],li,[role=listitem],article';
    const recordOf = (el) => { for (let c = el; c; c = up(c)) if (c.matches(RECORD)) return c; return null; };
    const records = all(RECORD), index = new Map(records.map((r, i) => [r, i]));
    const owner = (el) => { const r = recordOf(blockOf(el)); return r ? index.get(r) : -1; };
    // The blocks in the observation's order, each with its record: masked together, as one text.
    const blocks = [];
    for (const [b, t] of readBlocks()) { const v = t.replace(/\s+/g, ' ').trim(); if (v) blocks.push([owner(b), v]); }
    // What the observation lists, with no cap: a field with what it holds, else what can be pressed.
    const items = [];
    for (const e of actionable(Infinity, accepted)[0]) {
      const h = holds(e), secret = hidesSecret(e);
      // a select that lists its options (never a secret one) gives them too, named as listed
      if ('value' in h || 'checked' in h) items.push([owner(e), 'field', nameOf(e), 'value' in h ? h.value : h.checked,
                                                       ...(e.tagName === 'SELECT' && !secret ? [[...e.options].map(optionName)] : [])]);
      else if (secret && editable(e)) items.push([owner(e), 'field', nameOf(e), NOT_SHOWN]);
      else items.push([owner(e), 'press', nameOf(e), null]);
    }
    const used = new Set(blocks.map(b => b[0]).concat(items.map(i => i[0])));
    if (records.filter((_r, i) => used.has(i)).length > %(MAX_ROWS)d) complete = false;
    return {data: JSON.stringify({records: records.length, blocks, items,
                                  complete: complete && roots.length < 5000})};
  }
  // The first evaluation of a search: what it may look at, nothing a listing would not show.
  if (ask && ask.searchable) {
    let items = [];
    actionable(MAX_EL, accepted, (inOrder) => {
      items = inOrder.map((e) => { const v = holds(e).value;
        return {name: nameOf(e), value: typeof v === 'string' ? v : null,
                options: e.tagName === 'SELECT' && !hidesSecret(e) ? [...e.options].map(optionName) : []}; });
      return new Map(); });
    return {data: JSON.stringify({items})};
  }
  // The second: only ordinals, from one match made outside the page.
  const foundOnes = new Set(), foundOptions = new Map();
  const ahead = ask ? (inOrder) => { const first = new Map();
    for (const i of ask.near) if (inOrder[i]) first.set(inOrder[i], -1);
    for (const i of ask.found) if (inOrder[i]) { first.set(inOrder[i], -2); foundOnes.add(inOrder[i]); }
    for (const [i, held] of ask.options) if (inOrder[i] && inOrder[i].tagName === 'SELECT') {
      first.set(inOrder[i], -2); foundOptions.set(inOrder[i], new Set(held)); }
    return first; } : undefined;
  let [found, semantic, , room, left] = actionable(MAX_EL, accepted, ahead);
  const kept = new Set(found);
  let foundLeft = [...foundOnes].filter((e) => !kept.has(e)).length;
  for (const [s, held] of foundOptions) if (!kept.has(s)) foundLeft += held.size;
  // A secret select lists no options, in a search or out of one: its choice is never the model's.
  const listsOptions = (e) => e.tagName === 'SELECT' && !hidesSecret(e);
  // The count tells what exists: a select left out leaves its options out too.
  for (const s of all('select')) if (!kept.has(s) && box(s) && seen(s) && listsOptions(s)) left += s.options.length;
  // A select's options, those found first: a long list must not cut off the one sought.
  const optionsOf = (s) => { const held = foundOptions.get(s) || new Set(), o = [...s.options].map((x, n) => [x, held.has(n)]);
    return o.filter((x) => x[1]).concat(o.filter((x) => !x[1])); };
  // A select's options take the room the rule kept for them, the shortest list first, so a huge
  // select leaves a small one whole.
  const listed = new Map();
  for (const s of found.filter(listsOptions).sort((a, b) => a.options.length - b.options.length)) {
    const n = Math.min(s.options.length, room);
    listed.set(s, n);
    room -= n;
    left += s.options.length - n;
  }
  for (const e of found) {
    const attrRole = (e.getAttribute('role') || '').toLowerCase();
    const role = !semantic.has(e) ? 'clickable' : (ROLES.has(attrRole) ? attrRole : tagRole(e));
    const field = editable(e);
    // The kind of a field whose value is not free text (slice 5g): a closed set, from its type;
    // a real <textarea> is multi-line (slice 6b), the one field that takes line breaks.
    const type = e.tagName === 'INPUT' ? (e.getAttribute('type') || '').toLowerCase() : '';
    const textarea = e.localName === 'textarea' && e.namespaceURI === 'http://www.w3.org/1999/xhtml';
    data.push({role, name: nameOf(e), href: e.tagName === 'A' ? e.href : null, field,
               kind: textarea ? %(MULTI_LINE)r : (KINDS.has(type) ? type : null), ...holds(e),
               found: foundOnes.has(e)});
    els.push(e);
    // A visible select's options, each a target for ``select`` (action model S2-8): an option
    // has no box of its own while the dropdown is closed, so it is listed with its select.
    if (listsOptions(e)) {
      const options = optionsOf(e);
      for (const [o, isFound] of options.slice(0, listed.get(e))) {
        data.push({role: 'option', name: optionName(o), href: null, field: false, found: isFound});
        els.push(o);
      }
      foundLeft += options.slice(listed.get(e)).filter((x) => x[1]).length;
    }
  }
  const blocks = readBlocks();
  const text = [...blocks.values()].map(v => v.replace(/\s+/g, ' ').trim()).filter(Boolean).join('\n');
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
                                text: cut(text, %(MAX_INPUT)d), text_cut: text.length > %(MAX_INPUT)d,
                                els: data, complete, left, whole: roots.length < 5000, found_left: foundLeft}),
          els, fields: fields.slice(0, MAX_MASK), frames};
}""".replace("/*CONTROLS*/", "const KINDS = new Set(%s);" % json.dumps(sorted(INPUT_KINDS))
            + CONTROLS_JS).replace("/*SECRET*/", SECRET_JS) % {
    "MAX_ELEMENTS": MAX_ELEMENTS, "MULTI_LINE": MULTI_LINE, "MAX_VALUE": MAX_VALUE,
    "MAX_INPUT": MAX_INPUT, "MAX_ROWS": MAX_ROWS}


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
    text_cut: bool = False    # its text went past masking's input limit
    left: int = 0             # actionable elements the cap left out of ``elements``
    whole: bool = True        # no limit left part of the page's controls unread
    found_left: int = 0       # of those a search's words matched, how many the cap left out


async def extract(frame, find: str | None = None) -> Extraction:
    """One evaluation of the fixed extraction code in ``frame`` (isolated world). With ``find``,
    one before it for what a search may look at, matched here (``search_ask``): the page never
    receives the words."""
    ask = None
    if find is not None:
        handle = await frame.evaluate_handle(EXTRACT, {"searchable": True})
        try:
            seen = json.loads(await (await handle.get_property("data")).json_value())
        finally:
            await handle.dispose()
        ask = search_ask(seen["items"], find)
    handle = await frame.evaluate_handle(EXTRACT, ask)
    try:
        data = json.loads(await (await handle.get_property("data")).json_value())

        async def items(name):
            arr = await handle.get_property(name)
            props = await arr.get_properties()
            return [props[k].as_element() for k in sorted(props, key=lambda x: int(x))
                    if k.isdigit()]

        return Extraction(data["href"], data["title"], data["text"], data["els"],
                          await items("els"), await items("fields"), await items("frames"),
                          bool(data["complete"]), data.get("origin"), bool(data.get("text_cut")),
                          int(data.get("left") or 0), bool(data.get("whole", True)),
                          int(data.get("found_left") or 0))
    finally:
        await handle.dispose()


async def rows(frame) -> tuple[dict, bool]:
    """One evaluation of ``EXTRACT`` in its rows mode in ``frame`` (isolated world): the frame's
    reading (``records``, the count of its records; ``blocks``, [record, text] in the reading's
    order; ``items``, [record, "field" | "press", name, value]; record -1 is no record), and
    whether no limit left any of it out."""
    data = json.loads((await frame.evaluate(EXTRACT, {"rows": True}))["data"])
    return {k: data[k] for k in ("records", "blocks", "items")}, bool(data["complete"])


def _share(text: str, got, starts: list[int]) -> list[str]:
    """Each block's part of one masked text: a masked region goes, replaced once, to the block it
    starts in, and is gone from any block it runs on into; past masking's input limit nothing is
    shown."""
    out, regions = [], list(zip(got.spans, got.replacements))
    read = len(text)
    if read > MAX_INPUT:            # where masking itself stopped reading (``mask_text``)
        read = MAX_INPUT
        while read and not text[read - 1].isspace():
            read -= 1
    for a, b in zip(starts, starts[1:] + [len(text) + 1]):
        b, piece, cur = min(b - 1, read), [], a
        for (s, e), rep in regions:
            if e <= cur or s >= b:
                continue
            if s >= a:
                piece += [text[cur:s], rep]
            cur = max(cur, min(e, b))
        piece.append(text[cur:b] if cur < b else "")
        out.append("".join(piece).strip())
    return out


def rows_text(found: list) -> tuple[str, bool, bool]:
    """The frames' readings as JSON lines, one per record that holds anything, in page order,
    then one marked ``outside`` for what no record holds: the text, whether masking's input
    limit left it whole, and whether anything was masked. Every frame's blocks are masked
    together, joined as an observation joins its text, so a value spanning blocks is masked
    whichever records they fall in; then each block goes to its record. A field's name and
    value and a pressable name are masked on their own, as an observation's labels are."""
    masked = False

    def m(v):
        nonlocal masked
        if not isinstance(v, str) or v == "[not shown]":
            return v
        got = mask_text(v, limit=10 ** 9)
        masked = masked or bool(got.spans)
        return got.text
    groups, blocks, owners = [], [], []
    for frame in found:
        recs = [{"text": [], "fields": [], "press": []} for _ in range(int(frame["records"]))]
        outside = {"text": [], "fields": [], "press": [], "outside": True}
        for ri, t in frame["blocks"]:
            blocks.append(t)
            owners.append(recs[ri] if ri >= 0 else outside)
        for ri, kind, name, value, *options in frame["items"]:
            to = recs[ri] if ri >= 0 else outside
            if kind == "field":
                to["fields"].append([m(name), m(value), *([[m(o) for o in options[0]]] if options else [])])
            else:
                to["press"].append(m(name))
        groups.append((recs, outside))
    text = "\n".join(blocks)
    starts, at = [], 0
    for t in blocks:
        starts.append(at)
        at += len(t) + 1
    got = mask_text(text, limit=10 ** 9)
    masked = masked or bool(got.spans)
    for owner, part in zip(owners, _share(text, got, starts)):
        if part:
            owner["text"].append(part)
    lines = []
    for recs, outside in groups:
        for r in [*recs, outside]:
            if r["text"] or r["fields"] or r["press"]:
                lines.append(json.dumps({**r, "text": " ".join(r["text"])}, ensure_ascii=False))
    whole = len(text) <= MAX_INPUT and len(lines) <= MAX_ROWS
    return "\n".join(lines[:MAX_ROWS]), whole, masked


async def _closed_shadow_roots(page) -> list | None:
    """The page's document's closed shadow roots, asked of the browser itself (CDP
    ``DOM.getDocument`` with ``pierce``); frames are not descended (they are blacked out whole).
    None: the browser could not be asked."""
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        finally:
            await cdp.detach()
    except Exception:
        return None
    roots, stack = [], [doc.get("root", {})]
    while stack:
        node = stack.pop()
        for shadow in node.get("shadowRoots", []) or []:
            if shadow.get("shadowRootType") == "closed":
                roots.append(shadow)
            else:
                stack.append(shadow)
        stack.extend(node.get("children", []) or [])
    return roots


async def _documents_with_closed_root(session_of, targets) -> tuple[bool, int]:
    """Across these targets' documents, the frames inside each too: whether any holds a closed
    shadow root, and how many documents that reached. A target the browser cannot be asked about is
    no document reached (a frame in its parent's process has no session of its own)."""
    closed, reached = False, 0
    for target in targets:
        try:
            cdp = await session_of(target)
            try:
                doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
            finally:
                await cdp.detach()
        except Exception:  # noqa: BLE001
            continue
        stack = [doc.get("root", {})]
        reached += 1
        while stack:
            node = stack.pop()
            for shadow in node.get("shadowRoots", []) or []:
                closed = closed or shadow.get("shadowRootType") == "closed"
                stack.append(shadow)
            if node.get("contentDocument"):
                reached += 1
                stack.append(node["contentDocument"])
            stack.extend(node.get("children", []) or [])
    return closed, reached


async def closed_shadow_any(page) -> bool:
    """Whether the page's document or the document of any of its frames has a closed shadow root,
    which nothing can read or search. Each frame's document must be reached, in its parent's
    process or through a session of its own: one that is not counts as yes, and so does a failure
    to ask."""
    frames = child_frames(page)
    closed, reached = await _documents_with_closed_root(page.context.new_cdp_session,
                                                        [page, *frames])
    return closed or reached < 1 + len(frames)


async def closed_shadow_holds_field(page) -> bool:
    """Whether a closed shadow root holds a form field, in the shadow trees below it too. Nothing
    can read or mask such a field, so it keeps the page from a picture, a complete reading and
    the sign-in check. A root without one is a page's or an extension's own furniture (a password
    manager's button, a CAPTCHA's frame): its frames are covered like any frame. A failure to ask
    counts as yes."""
    roots = await _closed_shadow_roots(page)
    if roots is None:
        return True
    stack = list(roots)
    while stack:
        node = stack.pop()
        attributes = node.get("attributes") or []
        editable = dict(zip(attributes[::2], attributes[1::2])).get("contenteditable")
        if (node.get("nodeName", "").upper() in ("INPUT", "TEXTAREA", "SELECT")
                or (editable is not None and editable.lower() != "false")):
            return True
        stack += (node.get("children") or []) + (node.get("shadowRoots") or [])
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
_CROP_JS = """async ([b64, x, y, w, h, scale]) => {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const bmp = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
  const sx = bmp.width / innerWidth, sy = bmp.height / innerHeight;
  const cw = Math.round(w * sx), ch = Math.round(h * sy);
  const out = new OffscreenCanvas(Math.round(cw * scale), Math.round(ch * scale));
  out.getContext('2d').drawImage(bmp, Math.round(x * sx), Math.round(y * sy), cw, ch, 0, 0, out.width, out.height);
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
            clip["height"], 1]))
    except Exception:  # noqa: BLE001 — the crop is presentation; the whole image is the fallback
        return png, (0, 0)
    if not out.startswith(b"\x89PNG\r\n\x1a\n"):
        return png, (0, 0)
    return out, (clip["x"], clip["y"])


async def fit_png(page, png: bytes, limit: int) -> bytes:
    """``png`` (just captured, a whole or cropped window) smaller until it is within ``limit``: a
    window full of pictures is over it at full size. ``png`` itself when it fits or cannot be
    made smaller."""
    import base64
    import math
    for _try in range(3):
        if len(png) <= limit:
            break
        try:
            width, height = await page.evaluate("() => [innerWidth, innerHeight]")
            png = base64.b64decode(await page.evaluate(_CROP_JS, [
                base64.b64encode(png).decode(), 0, 0, width, height,
                0.9 * math.sqrt(limit / len(png))]))
        except Exception:  # noqa: BLE001 — the caller goes on without a picture
            break
    return png


class Observer:
    def __init__(self, registry: HandleRegistry, artifacts: Path, store=None,
                 extractor=extract, token=lambda: secrets.token_urlsafe(12),
                 closed_shadow=closed_shadow_holds_field, closed_any=closed_shadow_any,
                 rows_reader=rows):
        self.rows_reader = rows_reader
        self.closed_shadow = closed_shadow
        self.closed_any = closed_any
        self.registry = registry
        self.artifacts = artifacts
        self.store = store
        self.extractor = extractor
        self.token = token
        self.crop_origin = (0, 0)   # where the last stored screenshot began in the window

    async def _extract(self, frame, find: str | None = None) -> Extraction:
        """The extractor, with every browser-side failure as a typed code: a Patchright error's
        text can carry an address or a selector, so it never travels on."""
        try:
            return await self.extractor(frame) if find is None else await self.extractor(frame, find)
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

    async def _read_frames(self, run, session, policy: RunPolicy | None,
                           find: str | None = None, frames: list | None = None) -> tuple:
        """One reading of the page and its frames under the page-view gate; ``frames``, when
        given, receives the frame of each shown part, in the order of the parts."""
        frames = [] if frames is None else frames
        self._check_state(run, session)
        page = session.page
        generation = session.view_generation
        if not page_view_allowed(page.url, policy):
            raise ObservationRefused("page_off_session")
        main = await self._extract(page.main_frame, find)
        if main.href != page.url or not page_view_allowed(main.href, policy):
            raise ObservationRefused("page_off_session")
        texts = [main.text]
        parts = [(main, page.main_frame.url)]
        frames.append(page.main_frame)
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
                sub = await self.extractor(frame) if find is None else await self.extractor(frame, find)
            except Exception:
                views[id(frame)] = (False, None, None)
                texts.append(FRAME_WITHHELD)
                continue
            if frame_shown(address, sub.href, sub.origin, parent and parent[:2], policy):
                at = address if _web(address) else parent[2]
                views[id(frame)] = (True, sub.origin, at)
                texts.append(sub.text)
                parts.append((sub, at))
                frames.append(frame)
                continue
            views[id(frame)] = (False, sub.origin, None)
            texts.append(FRAME_WITHHELD)
        return page, generation, main, texts, parts

    async def observe_page(self, run, session, policy: RunPolicy | None, find: str | None = None
                           ) -> tuple[Observation, str, str]:
        """``observe``, plus the page's committed top-level address and its raw document
        title as this observation read them (controller-private: the run controller records a
        public-session page as a source from them, Task 12; neither reaches a model). With
        ``find``, what holds those words comes with handles wherever it is on the page."""
        page, generation, main, texts, parts = await self._read_frames(run, session, policy, find)
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
        # A frame not shown, an unread closed root or a limit reached left part of it unsearched.
        whole = None if find is None else (
            FRAME_WITHHELD not in texts and all(ex.whole for ex, _at in parts)
            and not await self.closed_any(page))
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
                label = _masked_name(info.get("name"))
                if not label:
                    counts[role] = counts.get(role, 0) + 1
                    label = f"(unnamed {role} {counts[role]})"
                href = info.get("href")
                target = PrivateTarget("link" if href else "element", role, el, href, frame_url,
                                       label)
                handle = self.registry.issue(run.run_id, session.site_id, generation, target)
                kind = info.get("kind")
                value = checked = None
                if id(info) in shown:
                    if isinstance(info.get("value"), str):
                        value = _masked_value(info["value"])
                    if isinstance(info.get("checked"), bool):
                        checked = info["checked"]
                elements.append(ElementView(handle, role, label[:300],
                                            kind if kind in FIELD_KINDS else None, value, checked,
                                            # one that answers secret now is not said to hold it
                                            bool(info.get("found")) and (
                                                "value" not in info or id(info) in shown)))
        page_target = PrivateTarget("page", "page", None, None, page.url)
        page_handle = self.registry.issue(run.run_id, session.site_id, generation, page_target)
        text, read_whole, _masked = mask_whole("\n".join(t for t in texts if t))
        left = sum(ex.left for ex, _at in parts)
        if left:
            text = CONTROLS_LEFT.format(count=left) + "\n" + text
        return (Observation(page_handle=page_handle, generation=generation, text=text[:MAX_TEXT],
                            elements=tuple(elements), source_id=None,
                            text_cut=(len(text) > MAX_TEXT or not read_whole
                                      or any(ex.text_cut for ex, _at in parts)),
                            controls_left=left, find_whole=whole,
                            find_left=sum(ex.found_left for ex, _at in parts)),
                main.href, main.title)

    async def read_text(self, run, session, policy: RunPolicy | None) -> tuple[str, bool, bool]:
        """The page's whole text as an observation reads it, no handle issued: the masked text,
        whether it is whole (no limit or incomplete reading), whether anything was masked."""
        page, generation, main, texts, parts = await self._read_frames(run, session, policy)
        if not self._same(session, page, generation, main.href):
            raise ObservationRefused("stale")
        text, whole, masked = mask_whole("\n".join(t for t in texts if t))
        return text, whole and all(ex.complete and not ex.text_cut for ex, _at in parts), masked

    async def read_rows(self, run, session, policy: RunPolicy | None) -> tuple[str, bool, bool]:
        """The page's rows (``EXTRACT``'s rows mode) as masked JSON lines, from the main frame and every frame
        an observation shows, no handle issued: the text, whether it is whole (no limit, no frame
        withheld, nothing unread), whether anything was masked."""
        frames: list = []
        page, generation, main, texts, _parts = await self._read_frames(run, session, policy,
                                                                        frames=frames)
        found, whole = [], FRAME_WITHHELD not in texts
        for frame in frames:
            try:
                got, complete = await self.rows_reader(frame)
            except Exception:  # noqa: BLE001 — a Patchright error's text can carry an address
                raise ObservationRefused("unavailable") from None
            found.append(got)
            whole = whole and complete
        if not self._same(session, page, generation, main.href):
            raise ObservationRefused("stale")
        text, read_whole, masked = rows_text(found)
        return text, whole and read_whole, masked

    async def _coverable(self, page, ex: Extraction) -> bool:
        """Whether the mask can cover this reading of the page: it is the page's own address,
        the inventory reached no limit, and no closed shadow root hides a field. Asked before
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
        """The operator's own screenshot (after a checked press or Enter, ``show_screenshot``):
        the page as it stands, masked as every screenshot is (``_capture``). A live view or a broker login
        raises ``ObservationRefused``; ``None`` when the page cannot be shown (a page that does
        not belong to the session is never captured)."""
        self._check_state(run, session)
        return await self._capture(run, session, policy)

    async def capture_for_model(self, run, session, policy: RunPolicy | None,
                                target=None) -> bytes | None:
        """A picture for a model (the check's, ``run_look``): the one capture rule, its target
        outlined, with no file and no store row. A live view or a broker login raises
        ``ObservationRefused``; ``None`` when it cannot be taken."""
        self._check_state(run, session)
        got = await self._capture_png(session, policy, target)
        return got and await fit_png(session.page, got[0], MAX_JUDGE_PICTURE_BYTES)

    async def _capture(self, run, session, policy: RunPolicy | None,
                       target=None) -> str | None:
        """An operator screenshot: the one capture rule, kept as a file with its store row."""
        got = await self._capture_png(session, policy, target)
        if got is None:
            return None
        png, self.crop_origin = got
        return self._keep(run, png)

    async def _capture_png(self, session, policy: RunPolicy | None,
                           target=None) -> tuple[bytes, tuple[int, int]] | None:
        """Every picture's one rule (slice 5h): only password, one-time-code and
        payment-card fields (the gate's one secret rule, ``hidesSecret``) and every child frame
        are masked; every other field shows what it holds. ``None`` when it cannot be faithful:
        the page is not the one read, the reading is incomplete or a closed shadow root hides a
        field (``_coverable``, before and again while the marks are on), or a secret field or a
        frame was not covered."""
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

        async def frames_of(extraction):
            # A frame in a closed shadow root is no part of the reading: the browser names it.
            return list(extraction.frame_handles) + [
                await frame.frame_element() for frame in child_frames(page)
                if frame.parent_frame is page.main_frame]

        nonce = self.token()
        marked: list = []
        png = None
        outline = None
        try:
            marked = await secret_fields(ex) + await frames_of(ex)
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
                for el in marked + await secret_fields(again) + await frames_of(again):
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
        return await crop_png(page, png, target)

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
