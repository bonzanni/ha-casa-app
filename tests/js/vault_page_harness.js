// Runs the secrets page's real script (casa/rootfs/opt/casa/vault_static/page.js) against the
// smallest stand-in for the browser objects it touches, and plays the moments a secret value is
// in flight: a Show or a Copy whose answer, or whose clipboard, settles only after the value was
// hidden. After the hiding event the value must be nowhere: not in the page, not in a node taken
// off it, not in an operation of the script, not handed to a clipboard call, not in a fallback
// text box. The server's next single-use code must still reach the form, and a code is never
// sent twice. Timers run on a simulated clock, so "after 30 seconds" is checked at 29,999 and
// 30,000 ms: a Show or Copy has 30 s from its tap, even when the page runs its timer late, a
// shown value 30 s from when it appears, and neither moves the other. A block runs one Show or
// Copy at a time, its Show and Copy are off only while its request is out, and, as in a browser,
// a disabled button takes no tap. A value never reaches the console. Given the add form, the
// edit form, the list and a page a Show answered as well, it also plays the forms' slots,
// suggestions, Replace and Remove/Undo, the list's Copy and the shown page's Hide. Plain Node,
// no dependencies; tests/test_vault_page_behaviour.py runs it.
//
// usage: node vault_page_harness.js <page.js> <bundle.json>
// bundle.json: {secret, other, values: {"<show url>#<field>": value}, pages: {name: tree}}, a
// tree being the page Page.handle rendered, as [tag, {attrs}, ...children | "text"].
// Prints one line per scenario, "ok <name>" or "FAIL <name><tab><why>"; exits 1 on any failure.
'use strict';
const fs = require('fs');
const vm = require('vm');
const SOURCE = fs.readFileSync(process.argv[2], 'utf8');
const BUNDLE = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const {secret: SECRET, other: OTHER, values: VALUES, pages: PAGES} = BUNDLE;
const HIDE_AT = 30000;

// Every operation the script begins is also kept here, so the harness can see whether one still
// holds a value. This is the only change made to the script, and it adds a line, nothing else.
const HOOK = 'const op = {block, value: null, over: false, until: null, timer: null};';
if (!SOURCE.includes(HOOK)) throw new Error('page.js no longer begins an operation as the harness expects');
const SCRIPT = SOURCE.replace(HOOK, `${HOOK} __ops.push(op);`);

// -- a selector engine for the selectors page.js uses: tag, #id, .class, [a], [a="v"], [a^="v"],
// descendants, and comma lists
const compound = (text) => {
  const m = /^([a-z]*)(?:#([\w-]+))?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/.exec(text);
  if (!m) throw new Error('selector not supported: ' + text);
  const classes = m[3].split('.').filter(Boolean);
  const attrs = [...m[4].matchAll(/\[([\w-]+)(?:(\^?=)"([^"]*)")?\]/g)];
  return (el) => (!m[1] || el.tag === m[1]) && (!m[2] || el.attrs.id === m[2])
    && classes.every((c) => el.classList.contains(c))
    && attrs.every(([, name, op, v]) => el.hasAttribute(name) && (!op
      || (op === '=' ? el.getAttribute(name) === v : el.getAttribute(name).startsWith(v))));
};
const matcher = (selector) => {
  const alternatives = selector.split(',').map((s) => s.trim().split(/\s+/).map(compound));
  return (el) => alternatives.some((parts) => {
    if (!parts[parts.length - 1](el)) return false;
    let i = parts.length - 2;
    for (let up = el.parent; up && i >= 0; up = up.parent) if (up.tag && parts[i](up)) i -= 1;
    return i < 0;
  });
};

class Text { constructor(text) { this.text = text; this.parent = null; } }

class Element {
  constructor(doc, tag, attrs = {}) {
    Object.assign(this, {doc, tag, attrs: {...attrs}, children: [], parent: null, listeners: {}});
    this._value = tag === 'input' ? attrs.value || '' : null;
    this.checked = 'checked' in attrs;
    this.disabled = 'disabled' in attrs;
    const el = this;
    this.classList = {
      contains: (c) => (el.attrs.class || '').split(/\s+/).includes(c),
      add: (c) => { if (!el.classList.contains(c)) el.className = `${el.className} ${c}`.trim(); },
      remove: (c) => { el.className = el.className.split(/\s+/).filter((x) => x !== c).join(' '); },
    };
    const kebab = (k) => `data-${String(k).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
    this.dataset = new Proxy({}, {
      get: (_, k) => el.attrs[kebab(k)],
      set: (_, k, v) => { el.attrs[kebab(k)] = String(v); return true; },
    });
  }
  hasAttribute(n) { return n in this.attrs; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  matches(s) { return matcher(s)(this); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get type() { return this.attrs.type || ''; }
  set type(v) { this.attrs.type = v; }
  get name() { return this.attrs.name || ''; }
  set name(v) { this.attrs.name = v; }
  get action() { return this.attrs.action || ''; }
  get value() { return this.tag === 'textarea' && this._value === null ? this.textContent : this._value || ''; }
  set value(v) { this._value = String(v); }
  get textContent() {
    return this.children.map((c) => (c instanceof Text ? c.text : c.textContent)).join('');
  }
  set textContent(v) {
    // every line a value block says is kept, so a line said twice is seen even when it is the same
    if (this.classList.contains('v-feedback')) this.doc.said.push(String(v));
    this.children.forEach((c) => { c.parent = null; }); this.children = []; this.append(new Text(String(v)));
  }
  get form() { return this.closest('form'); }
  append(...nodes) { for (const n of nodes) { if (n.parent) n.remove(); n.parent = this; this.children.push(n); } }
  appendChild(n) { this.append(n); return n; }
  insert(n, offset) {
    if (!this.parent) return;   // as in a browser: a node out of the page has no siblings
    if (n.parent) n.remove();
    const siblings = this.parent.children;
    siblings.splice(siblings.indexOf(this) + offset, 0, n);
    n.parent = this.parent;
  }
  after(n) { this.insert(n, 1); }
  before(n) { this.insert(n, 0); }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  replaceWith(n) { this.before(n); this.remove(); }
  closest(s) { const m = matcher(s); for (let el = this; el && el.tag; el = el.parent) if (m(el)) return el; return null; }
  *all() { for (const c of this.children) if (c instanceof Element) { yield c; yield* c.all(); } }
  querySelectorAll(s) { const m = matcher(s); return [...this.all()].filter(m); }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type) {
    const event = {type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  click() {
    // as in a browser, a disabled control takes no click
    if ((this.tag === 'button' || this.tag === 'input') && this.disabled) return;
    const event = this.dispatch('click');
    if (this.tag === 'button' && this.type === 'submit' && !event.defaultPrevented) this.form.submit();
  }
  submit() { this.doc.submitted += 1; }
  focus() { this.doc.activeElement = this; }
  select() {}
  setSelectionRange() {}
}

// -- one page in a fresh world, with fetch, the clipboard and the clock under the scenario's hand.
// BLOCK picks which of the page's Show/Copy forms the scenario taps (0: the first).
function world({clipboard = 'writeText', page = 'page', block = 0, visibility = 'visible'} = {}) {
  const doc = {submitted: 0, visibilityState: visibility, listeners: {}, activeElement: null, said: []};
  const build = ([tag, attrs, ...kids]) => {
    const el = new Element(doc, tag, attrs);
    // as a browser's parser does, a textarea and a pre drop the newline right after their tag
    if ((tag === 'textarea' || tag === 'pre') && typeof kids[0] === 'string') kids[0] = kids[0].replace(/^\n/, '');
    for (const k of kids) el.append(typeof k === 'string' ? new Text(k) : build(k));
    return el;
  };
  const root = build(PAGES[page]);
  Object.assign(doc, {
    body: root.querySelector('body'),
    createElement: (tag) => new Element(doc, tag),
    querySelector: (s) => root.querySelector(s),
    querySelectorAll: (s) => root.querySelectorAll(s),
    addEventListener: (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
  });
  const calls = [];   // [when, what, text]: what reached a clipboard or a fallback box, and when
  let hiddenYet = false;
  const log = (what, text) => calls.push([hiddenYet ? 'after' : 'before', what, String(text)]);
  const held = [];
  const hold = () => { let settle; const p = new Promise((ok, no) => { settle = {ok, no}; }); held.push(settle); return p; };
  const fetches = [];
  const requests = [];   // {url, field, code} of every request the script sent
  // the codes the server would take, each for the form action it was issued for: those on the
  // page for their own form, and each one it answered with for the path that asked
  const valid = new Map(root.querySelectorAll('input').filter((i) => i.name === 'code')
    .map((i) => [i.value, i.closest('form').action]));
  let writes = 0;   // how often clipboard.write was called
  const logged = [];   // every console call of the script, its arguments as text
  const console = new Proxy({}, {get: (_, how) => (...args) => logged.push([String(how), ...args.map((a) => {
    try { return typeof a === 'string' ? a : String(JSON.stringify(a)); } catch (err) { return String(a); }
  })])});
  let codes = 0;
  let now = 0;
  let timerIds = 0;
  const timers = new Map();   // id -> {due, fn}
  const windowListeners = {};
  const navigator = {};
  if (clipboard !== 'none') navigator.clipboard = {writeText: (t) => { log('writeText', t); return hold(); }};
  class ClipboardItem {
    constructor(items) { this.items = items; }
  }
  if (clipboard === 'ClipboardItem') {
    navigator.clipboard.write = ([item]) => {
      writes += 1;
      // a browser reads the blob when it is ready; a blob that is refused is never read
      Promise.resolve(item.items['text/plain']).then(async (blob) => log('ClipboardItem', await blob.text()), () => {});
      return hold();
    };
  }
  class FormData {
    constructor(form) {
      this.pairs = form.querySelectorAll('input').filter((i) => i.name && i.type !== 'checkbox').map((i) => [i.name, i.value]);
    }
    [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
  }
  const ops = [];
  const context = vm.createContext({
    document: doc, navigator, FormData, URLSearchParams, Blob, console, __ops: ops,
    performance: {now: () => now},
    ClipboardItem: clipboard === 'ClipboardItem' ? ClipboardItem : undefined,
    fetch: (url, options) => new Promise((ok, no) => fetches.push({url, body: String(options.body), ok, no})),
    setTimeout: (fn, ms) => { timerIds += 1; timers.set(timerIds, {due: now + ms, fn}); return timerIds; },
    clearTimeout: (id) => { timers.delete(id); },
    addEventListener: (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); },
  });
  vm.runInContext(SCRIPT, context, {filename: 'page.js'});
  const settle = () => new Promise((r) => setImmediate(r));
  const reveal = root.querySelectorAll('form.v-reveal')[block];
  const secretBlock = reveal && reveal.closest('[data-value-block]');
  const respond = async (status, body) => {
    fetches.shift().ok({status, json: async () => body});
    await w.flush();
  };
  const w = {
    root, doc, calls, settle, requests, fetches, logged, ops,
    writes: () => writes,
    // how often a value block was told TEXT
    saidTimes: (text) => doc.said.filter((line) => line === text).length,
    $: (s) => secretBlock.querySelector(s),
    code: () => secretBlock.querySelector('input[name="code"]').value,
    // The server: a code is taken once and only if it was handed out for the path it is sent
    // to; the value is the one stored for that path and field.
    async answer() {
      const {url, body} = fetches[0];
      const params = new URLSearchParams(body);
      const sent = {url, field: params.get('field'), code: params.get('code')};
      requests.push(sent);
      const good = valid.get(sent.code) === url;
      valid.delete(sent.code);
      const key = `${url}#${sent.field}`;
      if (!good) return respond(403, {});
      if (!(key in VALUES)) return respond(404, {});
      codes += 1;
      valid.set(`next-${codes}`, url);
      return respond(200, {value: VALUES[key], code: `next-${codes}`});
    },
    async refuse(status) {
      requests.push({url: fetches[0].url, code: new URLSearchParams(fetches[0].body).get('code')});
      return respond(status, {});
    },
    async lose() { fetches.shift().no(new TypeError('network')); await w.flush(); },
    async refuseClipboard() { while (held.length) held.shift().no(new Error('NotAllowedError')); await w.flush(); },
    async release() { while (held.length) held.shift().ok(); await w.flush(); },
    async flush() { for (let i = 0; i < 10; i++) await settle(); },
    hidingNow() { hiddenYet = true; },
    async tap(selector) { secretBlock.querySelector(selector).click(); await w.flush(); },
    // a tap, and nothing after it yet: what the script did inside the tap itself
    press(selector) { secretBlock.querySelector(selector).click(); },
    // move the clock on by MS, running every timer that falls due on the way, in order
    async advance(ms) {
      const until = now + ms;
      for (;;) {
        const due = [...timers].filter(([, t]) => t.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].due;
        due[1].fn();
        await w.flush();
      }
      now = until;
      await w.flush();
    },
    // move the clock on by MS and run no timer: the page was too busy to run them yet
    stall(ms) { now += ms; },
    async away() {
      doc.visibilityState = 'hidden';
      for (const fn of doc.listeners.visibilitychange || []) fn({});
      await w.flush();
      doc.visibilityState = 'visible';
      for (const fn of doc.listeners.visibilitychange || []) fn({});
    },
    async pagehide() { for (const fn of windowListeners.pagehide || []) fn({}); await w.flush(); },
    // the operations of the script that still hold a value
    holding: () => ops.filter((op) => op.value !== null),
    shown: () => secretBlock.querySelector('pre.v-revealed'),
    // everything the page holds as text, attribute values and box values included
    text() {
      const parts = [];
      const walk = (el) => {
        parts.push(...Object.values(el.attrs), el._value || '');
        for (const c of el.children) if (c instanceof Text) parts.push(c.text); else walk(c);
      };
      walk(root);
      return parts.join('\n');
    },
  };
  return w;
}

// After the hiding event: the value nowhere in the page, in no node taken off it (RETAINED), in
// no operation of the script, no clipboard call or fallback copy at all, no value box shown; and
// the code the server sent installed in the form.
const nothingKept = (w, code, retained = null) => {
  const why = [];
  if (w.text().includes(SECRET)) why.push('the value is in the page');
  if (retained && retained.textContent.includes(SECRET)) why.push('the value is in a node taken off the page');
  if (w.holding().length) why.push('an operation still holds a value');
  for (const [when, what, text] of w.calls) {
    if (when === 'after') why.push(`${what} ran${text.includes(SECRET) ? ' with the value' : ''}`);
  }
  if (w.$('pre.v-revealed')) why.push('a value box is shown');
  if (w.$('[data-hide-inline]')) why.push('a Hide is left with nothing to hide');
  if (w.$('.v-feedback') && w.$('.v-feedback').textContent === HOLD) why.push('it still says to press and hold the value');
  if (code && w.code() !== code) why.push(`the code is ${w.code()}, not ${code}`);
  if (w.doc.submitted) why.push('the form was sent');
  return why;
};

// While a value is shown: it is in exactly one place, the text of its own value box, and in no
// attribute or other box.
const onlyInItsBox = (w) => {
  const why = [];
  const places = [];
  const walk = (el) => {
    for (const [name, v] of Object.entries(el.attrs)) if (String(v).includes(SECRET)) places.push(`attribute ${name}`);
    if ((el._value || '').includes(SECRET)) places.push(`the value of a ${el.tag}`);
    for (const c of el.children) {
      if (!(c instanceof Text)) walk(c);
      else if (c.text.includes(SECRET)) places.push(el === w.shown() && c.text === SECRET ? 'its box' : `text in a ${el.tag}`);
    }
  };
  walk(w.root);
  if (JSON.stringify(places) !== '["its box"]') why.push(`the value is in: ${places.join(', ') || 'nowhere'}`);
  return why;
};

const FAILED = 'Could not load this value. Reload the page and try again.';
const TOO_LONG = 'This took too long. Reload the page and try again.';
const HOLD = 'Press and hold the value to copy it.';

// Copy tapped at 0 s and answered, its clipboard held; Show tapped and answered at 1 s; at 25 s
// the clipboard gives its OUTCOME. The Show's value goes at 31 s exactly, nothing is said over
// it, and its Hide is on throughout.
const copyThenShow = (outcome) => async (w) => {
  const why = [];
  const toggle = w.$('[data-action="show"]');
  await w.tap('[data-action="copy"]'); await w.answer();
  await w.advance(1000); await w.tap('[data-action="show"]');
  if (!w.ops[0].over || w.ops[0].value !== null) why.push('the Show did not end the Copy');
  await w.answer();
  if (!w.shown() || w.shown().textContent !== SECRET) return why.concat('the Show was not shown');
  const retained = w.shown();
  if (toggle.textContent !== 'Hide' || toggle.disabled) why.push('no working Hide once shown');
  await w.advance(24000); await outcome(w);
  if (w.$('.v-feedback').textContent) why.push(`after the clipboard it says "${w.$('.v-feedback').textContent}"`);
  if (toggle.disabled) why.push('Hide is off after the clipboard answered');
  await w.advance(HIDE_AT - 24000 - 1);
  if (!w.shown()) return why.concat('the value went before 30 s');
  w.hidingNow(); await w.advance(1);
  return why.concat(nothingKept(w, 'next-2', retained));
};
// two Copies, both answered and both waiting for the clipboard, then the page out of sight
const twoCopies = async (w) => {
  const why = [];
  await w.tap('[data-action="copy"]'); await w.answer();
  await w.tap('[data-action="copy"]');
  if (w.ops.length !== 2) return [`${w.ops.length} operations began`];
  if (!w.ops[0].over || w.ops[0].value !== null) why.push('the second Copy did not end the first');
  await w.answer();
  w.hidingNow(); await w.away();
  why.push(...nothingKept(w, 'next-2'));
  await w.refuseClipboard();
  return why.concat(nothingKept(w, 'next-2'));
};
const lateCopy = async (w) => {
  await w.tap('[data-action="copy"]'); w.hidingNow();
  w.stall(HIDE_AT + 2000); await w.answer(); await w.release(); await w.advance(0);
  const why = [];
  if (w.$('.v-feedback').textContent !== TOO_LONG) why.push(`it says "${w.$('.v-feedback').textContent}"`);
  await w.advance(HIDE_AT);
  if (w.saidTimes(TOO_LONG) !== 1) why.push(`it said it took too long ${w.saidTimes(TOO_LONG)} times`);
  return why.concat(nothingKept(w, 'next-1'));
};

const SCENARIOS = {
  'show pending, page hide event, answer': async (w) => {
    await w.tap('[data-action="show"]'); w.hidingNow(); await w.pagehide(); await w.answer();
    return nothingKept(w, 'next-1');
  },
  'show pending, page hidden and back, answer': async (w) => {
    await w.tap('[data-action="show"]'); w.hidingNow(); await w.away(); await w.answer();
    return nothingKept(w, 'next-1');
  },
  'show done, the value goes at 30 s and not before': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    const retained = w.shown();
    await w.advance(HIDE_AT - 1);
    if (!w.shown()) return ['the value went before 30 s'];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, 'next-1', retained);
  },
  'copy pending on ClipboardItem, Hide, clipboard refuses': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    const retained = w.shown();
    await w.tap('[data-action="copy"]');
    w.hidingNow(); await w.tap('[data-action="show"]');
    const why = nothingKept(w, 'next-1', retained);   // the clipboard still has not answered
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1', retained));
  },
  'copy pending on writeText, Hide, clipboard refuses': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    const retained = w.shown();
    await w.tap('[data-action="copy"]');
    w.hidingNow(); await w.tap('[data-action="show"]');
    const why = nothingKept(w, 'next-1', retained);   // the clipboard still has not answered
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1', retained));
  },
  'copy pending on the answer (no clipboard), page hidden and back, answer': async (w) => {
    await w.tap('[data-action="copy"]'); w.hidingNow(); await w.away(); await w.answer();
    return nothingKept(w, 'next-1');
  },
  'copy pending on ClipboardItem and the answer, page hidden and back, answer, refuses': async (w) => {
    await w.tap('[data-action="copy"]'); w.hidingNow(); await w.away(); await w.answer();
    const why = nothingKept(w, 'next-1');   // the clipboard still has not answered
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1'));
  },
  'copy pending on writeText and the answer, page hidden and back, answer, refuses': async (w) => {
    await w.tap('[data-action="copy"]'); w.hidingNow(); await w.away(); await w.answer();
    const why = nothingKept(w, 'next-1');
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1'));
  },
  'copy on writeText, the clipboard waits past 30 s, then refuses': async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer();
    await w.advance(HIDE_AT - 1);
    w.hidingNow(); await w.advance(1);
    const why = nothingKept(w, 'next-1');   // the clipboard still has not answered
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1'));
  },
  'copy on ClipboardItem, the clipboard waits past 30 s, then refuses': async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer();
    await w.advance(HIDE_AT - 1);
    w.hidingNow(); await w.advance(1);
    const why = nothingKept(w, 'next-1');
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1'));
  },
  'a shown value copied at 20 s (no clipboard) still goes at 30 s': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    const retained = w.shown();
    await w.advance(20000); await w.tap('[data-action="copy"]');
    const why = [];
    if (w.$('.v-feedback').textContent !== 'Press and hold the value to copy it.') why.push('no press-and-hold line');
    if (w.holding().length) why.push('the finished copy still holds the value');
    await w.advance(HIDE_AT - 20000 - 1);
    if (!w.shown()) why.push('the value went before 30 s');
    w.hidingNow(); await w.advance(1);
    return why.concat(nothingKept(w, 'next-1', retained));
  },
  'show, Hide, Show again sends the code the first answer brought': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    await w.tap('[data-action="show"]');
    await w.tap('[data-action="show"]'); await w.answer();
    const why = [];
    if (JSON.stringify(w.requests.map((r) => r.code)) !== JSON.stringify([w.requests[0].code, 'next-1'])) why.push(`codes sent: ${w.requests.map((r) => r.code)}`);
    if (!w.shown() || w.shown().textContent !== SECRET) why.push('the second Show was not answered');
    if (w.code() !== 'next-2') why.push(`the code is ${w.code()}`);
    return why;
  },
  // Every Show or Copy that fetches has its own deadline, 30 s from its tap, and a value that is
  // displayed has its own, 30 s from when it is displayed: neither moves or cuts short the other.
  'show answered after 45 s shows nothing, says it took too long, and the next code arrives': async (w) => {
    await w.tap('[data-action="show"]');
    await w.advance(HIDE_AT - 1);
    const why = [];
    if (w.$('.v-feedback').textContent) why.push(`at 29.999 s it says "${w.$('.v-feedback').textContent}"`);
    w.hidingNow(); await w.advance(1);
    if (w.$('.v-feedback').textContent !== TOO_LONG) why.push(`at 30 s it says "${w.$('.v-feedback').textContent}"`);
    await w.advance(15000); await w.answer();
    if (w.$('.v-feedback').textContent !== TOO_LONG) why.push(`after the answer it says "${w.$('.v-feedback').textContent}"`);
    if (w.saidTimes(TOO_LONG) !== 1) why.push(`it said it took too long ${w.saidTimes(TOO_LONG)} times`);
    return why.concat(nothingKept(w, 'next-1'));
  },
  'copy past 30 s whose answer is refused says it took too long and sends nothing again': async (w) => {
    await w.tap('[data-action="copy"]'); await w.advance(35000); await w.refuse(409); await w.release();
    const why = [];
    if (w.$('.v-feedback').textContent !== TOO_LONG) why.push(`it says "${w.$('.v-feedback').textContent}"`);
    await w.tap('[data-action="copy"]');
    if (w.fetches.length) why.push('a second request was sent');
    return why.concat(nothingKept(w, null));
  },
  'a finished Copy does not cut short a Show tapped 25 s later': async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.release();
    await w.advance(25000); await w.tap('[data-action="show"]'); await w.advance(6000); await w.answer();
    if (!w.shown() || w.shown().textContent !== SECRET) return [`the Show was not answered; it says "${w.$('.v-feedback').textContent}"`];
    const retained = w.shown();
    await w.advance(HIDE_AT - 1);
    if (!w.shown()) return ['the value went before 30 s'];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, 'next-2', retained);
  },
  'a Show answered at 29 s stays 30 s from then': async (w) => {
    await w.tap('[data-action="show"]'); await w.advance(29000); await w.answer();
    const retained = w.shown();
    await w.advance(HIDE_AT - 1);
    if (!w.shown()) return [`the value went before 30 s; it says "${w.$('.v-feedback').textContent}"`];
    if (w.$('.v-feedback').textContent) return [`it says "${w.$('.v-feedback').textContent}"`];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, 'next-1', retained);
  },
  'show, Hide at 5 s, Show at 25 s answered at 31 s: the second value stays its own 30 s': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    await w.advance(5000); await w.tap('[data-action="show"]');
    await w.advance(20000); await w.tap('[data-action="show"]');
    await w.advance(6000); await w.answer();   // past where the first value's 30 s ended
    if (!w.shown() || w.shown().textContent !== SECRET) return [`the second Show was not shown; it says "${w.$('.v-feedback').textContent}"`];
    const retained = w.shown();
    await w.advance(HIDE_AT - 1);
    if (!w.shown()) return ['the second value went before 30 s'];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, 'next-2', retained);
  },
  // A block has one operation at a time: a Show tapped while a Copy waits for the clipboard ends
  // the Copy, whose clipboard outcome then changes nothing on the page.
  'copy pending on the clipboard, then Show, the clipboard refuses: the value goes 30 s after it appeared': copyThenShow((w) => w.refuseClipboard()),
  'copy pending on ClipboardItem, then Show, the clipboard refuses: the value goes 30 s after it appeared': copyThenShow((w) => w.refuseClipboard()),
  'copy pending on the clipboard, then Show, the clipboard copies: the value goes 30 s after it appeared': copyThenShow((w) => w.release()),
  'copy pending on the clipboard, Show pending, the clipboard refuses: nothing shows before the Show, whose Hide works': async (w) => {
    const why = [];
    const toggle = w.$('[data-action="show"]');
    await w.tap('[data-action="copy"]'); await w.answer();   // its clipboard has not answered
    await w.tap('[data-action="show"]');   // its answer has not come
    await w.refuseClipboard();
    if (w.shown()) why.push('the Copy showed its value after the Show was tapped');
    if (w.$('.v-feedback').textContent) why.push(`it says "${w.$('.v-feedback').textContent}"`);
    if (toggle.textContent === 'Hide' && toggle.disabled) why.push('a Hide that is off');
    await w.answer();
    if (!w.shown() || toggle.textContent !== 'Hide' || toggle.disabled) return why.concat('the Show left no working Hide');
    const retained = w.shown();
    w.hidingNow(); await w.tap('[data-action="show"]');
    return why.concat(nothingKept(w, 'next-2', retained));
  },
  'two Copies pending on the clipboard: the second ends the first, and hiding the page leaves nothing': twoCopies,
  'two Copies pending on ClipboardItem: the second ends the first, and hiding the page leaves nothing': twoCopies,
  // The 30 s a Show or Copy has is a time, not only a timer: an answer handled after it, while
  // the page was too busy to run the timer, is dropped all the same.
  'show answered after its 30 s, before its timer ran, shows nothing and says it took too long': async (w) => {
    await w.tap('[data-action="show"]');
    w.stall(HIDE_AT + 2000); w.hidingNow(); await w.answer();
    const why = [];
    if (w.$('.v-feedback').textContent !== TOO_LONG) why.push(`it says "${w.$('.v-feedback').textContent}"`);
    await w.advance(0);   // the timer's turn: nothing more happens
    if (w.$('.v-feedback').textContent !== TOO_LONG) why.push(`then it says "${w.$('.v-feedback').textContent}"`);
    if (w.saidTimes(TOO_LONG) !== 1) why.push(`it said it took too long ${w.saidTimes(TOO_LONG)} times`);
    return why.concat(nothingKept(w, 'next-1'));
  },
  'copy answered after its 30 s, before its timer ran, copies nothing': lateCopy,
  'copy on ClipboardItem answered after its 30 s, before its timer ran, copies nothing': lateCopy,
  'the tapped button is off while its request is out and on again once it is answered': async (w) => {
    const why = [];
    const toggle = w.$('[data-action="show"]');
    const copy = w.$('[data-action="copy"]');
    w.press('[data-action="show"]');
    if (!toggle.disabled) why.push('Show is on while its request is out');
    await w.flush(); await w.answer();
    if (toggle.textContent !== 'Hide' || toggle.disabled) why.push('no working Hide after the answer');
    await w.tap('[data-action="show"]');
    if (w.shown()) why.push('Hide did not hide');
    w.press('[data-action="copy"]');
    if (!copy.disabled) why.push('Copy is on while its request is out');
    await w.flush(); await w.answer(); await w.release();
    if (copy.disabled) why.push('Copy is off after the answer');
    if (w.$('.v-feedback').textContent !== 'Copied to clipboard.') why.push(`it says "${w.$('.v-feedback').textContent}"`);
    return why;
  },
  'copy on ClipboardItem calls the clipboard once, inside the tap': async (w) => {
    w.press('[data-action="copy"]');
    const why = [];
    if (w.writes() !== 1) why.push(`clipboard.write was called ${w.writes()} times inside the tap`);
    if (w.fetches.length !== 1) why.push('the value was not asked for inside the tap');
    await w.flush(); await w.answer(); await w.release();
    if (w.writes() !== 1) why.push(`clipboard.write was called ${w.writes()} times in all`);
    if (!w.calls.some(([, what, text]) => what === 'ClipboardItem' && text === SECRET)) why.push('the clipboard did not get the value');
    if (w.$('.v-feedback').textContent !== 'Copied to clipboard.') why.push(`it says "${w.$('.v-feedback').textContent}"`);
    return why;
  },
  // controls: the harness sees a value where one should be, and only there
  'control: show displays the value': async (w) => {
    await w.tap('[data-action="show"]'); await w.answer();
    const why = onlyInItsBox(w);
    if (!w.shown() || w.shown().textContent !== SECRET || w.code() !== 'next-1') why.push('the value was not shown');
    if (w.holding().length) why.push('the finished Show still holds the value outside the page');
    return why;
  },
  'control: copy hands the value to the clipboard': async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.release();
    const why = [];
    if (!(w.calls.some(([, what, text]) => what === 'writeText' && text === SECRET) && w.code() === 'next-1')) why.push('the clipboard did not get the value');
    if (w.holding().length) why.push('the finished copy still holds the value');
    return why;
  },
};
// While a form's code is out, its Show and its Copy are both off: a tap on the other one is not
// taken (and so not dropped unseen). Both are on again once the request ends, answered or not.
for (const [first, other] of [['show', 'copy'], ['copy', 'show']]) {
  for (const [how, finish] of [['answered', (w) => w.answer()], ['refused', (w) => w.refuse(409)]]) {
    SCENARIOS[`${first} out: ${other} is off and takes no tap; both on once it is ${how}`] = async (w) => {
      const why = [];
      const tapped = w.$(`[data-action="${first}"]`);
      const second = w.$(`[data-action="${other}"]`);
      w.press(`[data-action="${first}"]`);
      if (!tapped.disabled) why.push(`${first} is on while its request is out`);
      if (!second.disabled) why.push(`${other} is on while the ${first} request is out`);
      await w.flush();
      await w.tap(`[data-action="${other}"]`);
      if (w.ops.length !== 1) why.push(`${w.ops.length} operations began`);
      if (w.fetches.length !== 1) why.push(`${w.fetches.length} requests are out`);
      if (w.$('.v-feedback').textContent) why.push(`the tap on ${other} says "${w.$('.v-feedback').textContent}"`);
      await finish(w); await w.release();
      if (tapped.disabled) why.push(`${first} is off once its request is ${how}`);
      if (second.disabled) why.push(`${other} is off once the ${first} request is ${how}`);
      return why;
    };
  }
}

// A failed Show or Copy: the code it sent may be spent, so the form sends it never again and
// asks for a reload.
for (const [where, page, block, action] of [['show', 'page', 0, 'show'], ['copy', 'page', 0, 'copy'],
  ['list copy', 'list', 2, 'copy']]) {
  for (const [how, fail] of [['refused (409)', (w) => w.refuse(409)], ['lost', (w) => w.lose()]]) {
    SCENARIOS[`${where} ${how}, tapped again: nothing is sent twice`] = Object.assign(async (w) => {
      await w.tap(`[data-action="${action}"]`); await fail(w); await w.release();
      const why = [];
      if (w.$('.v-feedback').textContent !== FAILED) why.push(`it says "${w.$('.v-feedback').textContent}"`);
      if (w.$(`[data-action="${action}"]`).disabled) why.push('the button is still off');
      await w.tap(`[data-action="${action}"]`);
      if (w.fetches.length) why.push('a second request was sent');
      if (w.$('.v-feedback').textContent !== FAILED) why.push('the reload line went');
      if (w.holding().length) why.push('an operation holds a value');
      return why;
    }, {page, block});
  }
}

// -- the forms, the list and the page a Show answered
const slot = (w, n) => w.root.querySelector(`input[name="add${n}"]`).closest('section');
const toggle = (box, on) => { box.checked = on; box.dispatch('change'); };
const sent = (w, why) => (w.doc.submitted ? why.concat('the form was sent') : why);
// the name=value pairs a browser would send for FORM, in order: every named input and text
// area that is not disabled, a checkbox only when ticked
const pairs = (form) => JSON.stringify([...form.all()]
  .filter((el) => el.name && !el.disabled && (el.tag === 'textarea'
    || (el.tag === 'input' && (el.type !== 'checkbox' || el.checked))))
  .map((el) => [el.name, el.value]));
// Each extra slot of a form, opened, closed and opened again: open, the form sends exactly what it
// sent before plus that slot's add<N>, label<N> and value<N> as typed; closed, exactly what it
// sent before.
const slotsSend = (slots) => async (w) => {
  const why = [];
  const form = w.root.querySelector('#secret-form');
  const before = pairs(form);
  for (const n of slots) {
    const box = w.root.querySelector(`input[name="add${n}"]`);
    const open = JSON.stringify([...JSON.parse(before), [`add${n}`, '1'], [`label${n}`, `label-${n}`],
      [`value${n}`, `value-${n}`]]);
    toggle(box, true);
    slot(w, n).querySelector('input[type="text"]').value = `label-${n}`;
    slot(w, n).querySelector('textarea').value = `value-${n}`;
    for (const [on, want] of [[true, open], [false, before], [true, open], [false, before]]) {
      toggle(box, on);
      if (pairs(form) !== want) why.push(`slot ${n} ${on ? 'open' : 'closed'} sends ${pairs(form)}`);
    }
  }
  return sent(w, why);
};
const PAGE_SCENARIOS = {
  'add form, a closed slot sends nothing, opening it turns its boxes on and focuses its value': [{page: 'add'}, async (w) => {
    const why = [];
    const second = slot(w, 1);
    const boxes = second.querySelectorAll('input[type="text"], textarea');
    if (boxes.some((box) => !box.disabled)) why.push('a closed slot has a box that is sent');
    if (w.root.querySelector('textarea[name="value0"]').disabled) why.push('the first slot is off');
    toggle(second.querySelector('input[name="add1"]'), true);
    if (boxes.some((box) => box.disabled)) why.push('an opened slot has a box that is off');
    if (w.doc.activeElement !== second.querySelector('textarea')) why.push('the value box has no focus');
    return sent(w, why);
  }],
  'add form, closing a slot keeps what was typed and sends none of it': [{page: 'add'}, async (w) => {
    const why = [];
    const second = slot(w, 1);
    const open = second.querySelector('input[name="add1"]');
    toggle(open, true);
    second.querySelector('textarea').value = 'typed';
    toggle(open, false);
    if (second.querySelector('textarea').value !== 'typed') why.push('the draft was lost');
    if (!second.querySelector('textarea').disabled) why.push('the closed slot is still sent');
    if (w.doc.activeElement !== open) why.push('the focus is not on the slot\'s box');
    toggle(open, true);
    if (second.querySelector('textarea').value !== 'typed') why.push('the draft did not come back');
    return sent(w, why);
  }],
  'add form, each extra slot sends its own label and value while open and nothing while closed': [{page: 'add'}, slotsSend([1, 2])],
  'edit form, each extra slot sends its own label and value while open and nothing while closed': [{page: 'edit'}, slotsSend([2, 3, 4])],
  'add form, a suggestion fills the label and marks itself, typing unmarks it': [{page: 'add'}, async (w) => {
    const why = [];
    const label = w.root.querySelector('input[name="label0"]');
    const group = label.closest('[data-new-value]').querySelector('.v-picks');
    if (group.hidden) why.push('the suggestions are not shown');
    const pressed = () => group.querySelectorAll('[data-label]').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.label);
    group.querySelector('[data-label="username"]').click();
    if (label.value !== 'username') why.push(`the label is ${label.value}`);
    if (JSON.stringify(pressed()) !== '["username"]') why.push(`pressed: ${pressed()}`);
    if (w.doc.activeElement === label) why.push('the label box took the focus');
    label.value = 'user'; label.dispatch('input');
    if (pressed().length) why.push('a suggestion stays marked for a typed label');
    return sent(w, why);
  }],
  'add form, a slip is said before anything is sent': [{page: 'add'}, async (w) => {
    const why = [];
    const form = w.root.querySelector('#secret-form');
    const event = form.dispatch('submit');
    if (!event.defaultPrevented) why.push('the form was sent without a name');
    const box = w.root.querySelector('#form-error');
    if (box.hidden || !box.textContent.includes('Enter a name.')) why.push(`the box says "${box.textContent}"`);
    // a named secret whose first value is empty, another value's draft in an open slot
    w.root.querySelector('input[name="name"]').value = 'Mail';
    const second = slot(w, 1);
    toggle(second.querySelector('input[name="add1"]'), true);
    second.querySelector('input[type="text"]').value = 'user';
    second.querySelector('textarea').value = 'draft';
    if (!form.dispatch('submit').defaultPrevented) why.push('the form was sent with an empty value');
    const empty = w.root.querySelector('textarea[name="value0"]');
    if (!box.textContent.includes('Enter a value.')) why.push(`for an empty value the box says "${box.textContent}"`);
    if (empty.getAttribute('aria-invalid') !== 'true' || w.doc.activeElement !== empty) why.push('the empty value is not the one pointed at');
    if (second.querySelector('textarea').value !== 'draft') why.push('the other value\'s draft was lost');
    empty.value = 'v';
    if (form.dispatch('submit').defaultPrevented) why.push('a good form was held back');
    return sent(w, why);
  }],
  'edit form, Replace turns the new-value box on and keeps its draft when turned off': [{page: 'edit'}, async (w) => {
    const why = [];
    const tick = w.root.querySelector('input[name^="replace"]');
    const box = tick.closest('section').querySelector('textarea');
    if (!box.disabled) why.push('the box is sent before Replace');
    toggle(tick, true);
    if (box.disabled) why.push('the box is off after Replace');
    if (w.doc.activeElement !== box) why.push('the box has no focus');
    box.value = SECRET;
    toggle(tick, false);
    if (!box.disabled) why.push('the box is still sent after Replace is turned off');
    if (box.value !== SECRET) why.push('the draft was lost');
    return sent(w, why);
  }],
  // Undo gives back exactly what the form sent before Remove, a typed draft included
  'edit form, Remove turns a value off and Undo turns it back as it was': [{page: 'edit'}, async (w) => {
    const why = [];
    const form = w.root.querySelector('#secret-form');
    const card = w.root.querySelector('input[name^="replace"]').closest('section');
    const replace = card.querySelector('.v-replace-input');
    toggle(replace, true);
    card.querySelector('textarea').value = 'replacement draft';
    const plain = w.root.querySelector('.v-existing textarea[name="value0"]');
    plain.value = 'plain draft';
    for (const [what, box] of [['replacement', card.querySelector('textarea')], ['plain', plain]]) {
      const remove = box.closest('section').querySelector('.v-remove-input');
      const before = pairs(form);
      if (!before.includes(`${what} draft`)) why.push(`the ${what} draft is not sent before Remove`);
      toggle(remove, true);
      if (!box.disabled) why.push(`a removed ${what} value is still sent`);
      if (what === 'replacement' && !replace.disabled) why.push('a removed value still sends Replace');
      toggle(remove, false);
      if (pairs(form) !== before) why.push(`after Undo of the ${what} value the form sends ${pairs(form)}, not ${before}`);
    }
    return sent(w, why);
  }],
  // the list has two rows with a value of its own before this one: Copy must ask for this row's
  // value, with this row's code, and get nothing else
  'list, Copy hands the value to the clipboard and says so': [{page: 'list', block: 2}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.release();
    const why = [];
    if (!w.calls.some(([, what, text]) => what === 'writeText' && text === SECRET)) why.push('the clipboard did not get the value');
    if (w.calls.some(([, , text]) => text.includes(OTHER))) why.push('the clipboard got another row\'s value');
    if (w.requests.length !== 1 || w.requests[0].field !== '0') why.push(`requests: ${JSON.stringify(w.requests)}`);
    if (w.$('[data-action="copy"]').textContent !== 'Copied') why.push('the button does not say Copied');
    if (w.$('.v-feedback').textContent !== 'Copied to clipboard.') why.push('no status line');
    if (w.text().includes(SECRET)) why.push('the value is in the page');
    if (w.code() !== 'next-1') why.push('the next code did not arrive');
    if (w.holding().length) why.push('the finished copy still holds the value');
    return why;
  }],
  'list, a refused clipboard shows the value in the row until its Hide': [{page: 'list', block: 2}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.refuseClipboard();
    const why = [];
    if (!w.shown() || w.shown().textContent !== SECRET) why.push('the value is not shown');
    else why.push(...onlyInItsBox(w));
    if (w.$('.v-feedback').textContent !== 'Press and hold the value to copy it.') why.push('no press-and-hold line');
    const hide = w.$('[data-hide-inline]');
    if (!hide) return why.concat('no Hide');
    const retained = w.shown();
    w.hidingNow(); hide.click(); await w.flush();
    return why.concat(nothingKept(w, 'next-1', retained));
  }],
  // the clipboard refuses 10 s after the tap: the value is shown from then, for 30 s
  'list, a value shown after a refused clipboard goes at 30 s and not before': [{page: 'list', block: 2}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.advance(10000); await w.refuseClipboard();
    const retained = w.shown();
    await w.advance(HIDE_AT - 1);
    if (!w.shown()) return ['the value went before 30 s'];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, 'next-1', retained);
  }],
  'list, a shown value copied at 20 s (no clipboard) still goes at 30 s': [{page: 'list', block: 2, clipboard: 'none'}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer();
    const retained = w.shown();
    await w.advance(20000); await w.tap('[data-action="copy"]');
    await w.advance(HIDE_AT - 20000 - 1);
    if (!w.shown()) return ['the value went before 30 s'];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, 'next-1', retained);
  }],
  'list, a shown value goes when the page is hidden': [{page: 'list', block: 2}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.refuseClipboard();
    const retained = w.shown();
    w.hidingNow(); await w.away();
    return nothingKept(w, 'next-1', retained);
  }],
  'list, copy pending on the answer, page hide event, answer': [{page: 'list', block: 2}, async (w) => {
    await w.tap('[data-action="copy"]'); w.hidingNow(); await w.pagehide(); await w.answer();
    const why = nothingKept(w, 'next-1');
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1'));
  }],
  'list, copy on writeText, the clipboard waits past 30 s, then refuses': [{page: 'list', block: 2}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer();
    await w.advance(HIDE_AT - 1);
    w.hidingNow(); await w.advance(1);
    const why = nothingKept(w, 'next-1');
    await w.refuseClipboard();
    return why.concat(nothingKept(w, 'next-1'));
  }],
  'list, Copy of an empty value copies nothing and says Empty': [{page: 'list', block: 1}, async (w) => {
    await w.tap('[data-action="copy"]'); await w.answer(); await w.release();
    const why = [];
    if (w.calls.length) why.push(`the clipboard was called: ${JSON.stringify(w.calls)}`);
    if (!w.shown() || w.shown().textContent !== 'Empty') why.push('it does not say Empty');
    if (w.$('.v-feedback').textContent) why.push(`it says "${w.$('.v-feedback').textContent}"`);
    return why;
  }],
  'shown page, its Hide link becomes a button that hides the value': [{page: 'shown'}, async (w) => {
    const why = [];
    if (w.$('a')) why.push('the Hide link is still there');
    const button = w.$('[data-action="show"]');
    if (!button || button.textContent !== 'Hide') return why.concat('no Hide button');
    const retained = w.shown();
    w.hidingNow(); await w.tap('[data-action="show"]');
    return why.concat(nothingKept(w, null, retained));
  }],
  'shown page, its value goes at 30 s and not before': [{page: 'shown'}, async (w) => {
    const retained = w.shown();
    await w.advance(HIDE_AT - 1);
    if (!w.shown()) return ['the value went before 30 s'];
    w.hidingNow(); await w.advance(1);
    return nothingKept(w, null, retained);
  }],
  'shown page loaded out of sight: the value is gone at once': [{page: 'shown', visibility: 'hidden'}, async (w) => nothingKept(w, null)],
};

const CLIPBOARD = {
  'copy pending on ClipboardItem, Hide, clipboard refuses': 'ClipboardItem',
  'copy pending on the answer (no clipboard), page hidden and back, answer': 'none',
  'copy pending on ClipboardItem and the answer, page hidden and back, answer, refuses': 'ClipboardItem',
  'copy on ClipboardItem, the clipboard waits past 30 s, then refuses': 'ClipboardItem',
  'a shown value copied at 20 s (no clipboard) still goes at 30 s': 'none',
  'copy on ClipboardItem calls the clipboard once, inside the tap': 'ClipboardItem',
  'copy pending on ClipboardItem, then Show, the clipboard refuses: the value goes 30 s after it appeared': 'ClipboardItem',
  'two Copies pending on ClipboardItem: the second ends the first, and hiding the page leaves nothing': 'ClipboardItem',
  'copy on ClipboardItem answered after its 30 s, before its timer ran, copies nothing': 'ClipboardItem',
};

// an error thrown in one of the page's handlers is the scenario's failure, as a browser would
// report it, not the end of the run
let thrown = [];
process.on('unhandledRejection', (err) => thrown.push(String(err)));
process.on('uncaughtException', (err) => thrown.push(String(err)));

(async () => {
  let failed = 0;
  const all = [
    ...Object.entries(SCENARIOS).map(([name, play]) => [name, {clipboard: CLIPBOARD[name], page: play.page, block: play.block}, play]),
    ...Object.entries(PAGE_SCENARIOS).map(([name, [options, play]]) => [name, options, play]),
  ];
  for (const [name, options, play] of all) {
    let why;
    thrown = [];
    let w;
    try { w = world(options); why = await play(w); } catch (err) { why = [String(err)]; }
    why = why.concat(thrown);
    if (w && w.logged.some((args) => args.some((a) => a.includes(SECRET) || a.includes(OTHER)))) why.push('a value was given to the console');
    if (why.length) failed += 1;
    console.log(why.length ? `FAIL ${name}\t${why.join('; ')}` : `ok ${name}`);
  }
  process.exit(failed ? 1 : 0);
})();
