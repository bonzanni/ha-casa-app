// Casa Browser live view ("W+"): the X screen stays 1920x1080; only Chrome's window is fitted to
// this viewer (at least 500 px wide, so a narrower phone scales the picture by width/500). Chrome's
// toolbar is cropped and replaced by our bar (site, Done); tapping the site shows it.
// noVNC is display-only: every input goes through the /ctl socket to the controller, which
// injects it at the X level. Typing (live view K): the phone is Chrome's keyboard — see the
// keyboard section below.
//
// The keyboard (casa-test F14): inside Home Assistant's frame the phone's keyboard shrinks the top
// page's visual viewport, not this page's; the viewer fits itself — bar, picture, typing bar —
// into the part of its frame the operator can still see, and asks Home Assistant again to hide
// its header at every viewport change (F29). One finger only: the picture zooms by its − · Fit · +
// buttons (F19); the page itself never zooms or scrolls.
//
// Reconnect: when the phone suspends the app (an app switch), both sockets die, sometimes
// without a close event. On return (visibilitychange, pageshow, online), on a close, or when the
// controller's heartbeat stops, the viewer reopens both sockets by itself and says so. It never
// claims anything: the run stays paused until Done.
const $ = s => document.querySelector(s);
const app = $('#app'), screenEl = $('#screen'), touch = $('#touch');
const PREFIX = app.dataset.prefix, RUN = app.dataset.run, VIEWER = app.dataset.viewer;
// The page's strings in the operator's language (ui.py fills data-strings from the catalog).
const STRINGS = (() => { try { return JSON.parse(app.dataset.strings || '{}'); } catch (err) { return {}; } })();
const T = (key, english) => (typeof STRINGS[key] === 'string' && STRINGS[key]) || english;
const PPN = 120;     // remote px per X wheel notch in Chrome (measured)
const MINW = 500;    // Chrome's narrowest window (measured): narrower phones scale
const BEAT_MS = 12000;
const wsBase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + PREFIX + '/view/' + RUN + '/';
const wsUrl = kind => wsBase + kind + '?v=' + encodeURIComponent(VIEWER);
const stats = window.__lv = {ctl: 0, vnc: 0, reconnects: 0, sent: [], states: 0, kiosk: 0,   // read by the tests
  status: () => $('#status').textContent, state: () => state, open: () => [!!(ctl && ctl.readyState === 1), rfbConnected],
  zoom: () => ({z: zoom.z, x: zoom.x, y: zoom.y}), box: () => visibleBox(),
  kb: () => ({sess: sess && {mode: sess.mode, ok: sess.ok, key: sess.key}, want, type: kb.type, focused: document.activeElement === kb}),
  at: (X, Y) => toClient(X, Y), trace: () => lines.slice(),
  // [row shown, row wanted] while the picture is visible; wanted is unknown (-1) before a state
  pan: () => (rfbConnected && screenEl.style.visibility !== 'hidden' ? [vp().y, CROP ? (state ? toolbarH() : -1) : 0] : null),
  toast: msg => toast(msg)};
let CROP = true;
// The picture shows, and takes taps, only from the first state on: until Chrome's toolbar height
// is known it could only be shown uncropped, then jump up by that height under a finger.
screenEl.style.visibility = 'hidden';
let ended = false;   // done, ended or opened elsewhere: no more reconnects

// ---------------------------------------------------------------- status
function setStatus(text) { $('#status').textContent = text; }
function refreshStatus() {
  if (ended) return;
  const ctlOk = ctl && ctl.readyState === 1, vncOk = rfbConnected;
  setStatus(ctlOk && vncOk && state ? '' : (everConnected ? T('js.reconnecting', 'Reconnecting…') : T('js.connecting', 'Connecting…')));
}
let everConnected = false;
function cover(text, link) {
  ended = true;
  $('#covertext').textContent = text;
  if (link) {
    const a = document.createElement('a'); a.href = PREFIX + '/'; a.textContent = T('js.home', 'Home');
    $('#covertext').append(document.createElement('br'), a);
  }
  $('#cover').hidden = false; setStatus(''); unkiosk();
}
async function askState() {
  try {
    const r = await fetch(PREFIX + '/view/' + RUN + '/state?v=' + encodeURIComponent(VIEWER), {credentials: 'same-origin', cache: 'no-store'});
    const {state: s} = await r.json();
    if (s === 'review') { ended = true; location.href = PREFIX + '/view/' + RUN + '/review'; return false; }
    if (s === 'elsewhere') { cover(T('js.elsewhere', 'This live view was opened somewhere else.'), true); return false; }
    if (s === 'ended') { cover(T('js.ended', 'This live view has ended.'), true); return false; }
  } catch (err) { /* offline: keep trying */ }
  return true;
}
function onServerClose(code) {
  // Ended — or opened somewhere else: the server says which (casa-test F8)
  if (code === 4001) askState().then(ok => { if (ok) cover(T('js.ended', 'This live view has ended.'), true); });
  else if (code === 4002) reconnectAll();   // another socket replaced this one: the server decides
  else if (code === 4003) { ended = true; location.href = PREFIX + '/view/' + RUN + '/review'; }
}

// ---------------------------------------------------------------- the control channel
let ctl = null, state = null, lastBeat = 0, ctlBackoff = 1000, ctlTimer = null;
const MAX_TEXT = 256;   // the controller types at most this many characters per message
function send(m) {
  stats.sent.push(m.x === undefined ? m.t : [m.t, m.x, m.y]);
  if (ctl && ctl.readyState === 1) ctl.send(JSON.stringify(m));
}
function openCtl() {
  if (ended) return;
  clearTimeout(ctlTimer);
  const ws = new WebSocket(wsUrl('ctl'));
  ctl = ws;
  ws.onopen = () => {
    if (ctl !== ws) return;
    stats.ctl++; ctlBackoff = 1000; lastBeat = Date.now(); everConnected = true; lastFit = '';
    // A new socket: what the box names is unconfirmed until a state answers this socket's
    // first message (its fit); a send remembered for the old socket goes (its text stays).
    // A new socket: an editing session does not carry over (tap the field again); text it
    // could not send is reported, and so is a batch the old socket never answered (Terra K d2).
    if (unresolved.size) { unresolved.clear(); kbDown(T('js.unsure', 'Not sure it was sent — check the field')); }
    // ...except the session a paste is pending for: iOS's paste prompt may send the page to the
    // background, and its text still goes to that field, bound and guarded (revision 7)
    else if (sess && sess.pasteHold) { trace('socket back: the paste keeps its session'); setTimeout(flush, 0); }
    else if (sess) kbDown();
    fit(true, ++seq); refreshStatus();
  };
  ws.onmessage = ev => {
    if (ctl !== ws) return;
    lastBeat = Date.now();
    let m; try { m = JSON.parse(ev.data); } catch (err) { return; }
    if (m.t === 'state') { stats.states++; onState(m); refreshStatus(); }
    else if (m.t === 'sel') onSel(m.text);
  };
  ws.onclose = ev => {
    if (ctl !== ws) return;
    ctl = null; refreshStatus();
    if (ended) return;
    if (ev.code >= 4001 && ev.code <= 4003) { onServerClose(ev.code); return; }
    const mine = attempt;
    ctlTimer = setTimeout(async () => { if (await askState() && mine === attempt && ctl === null) openCtl(); }, ctlBackoff);
    ctlBackoff = Math.min(8000, ctlBackoff * 2);
  };
}

// ---------------------------------------------------------------- the picture (noVNC, view only)
let RFB = null, rfb = null, rfbConnected = false, vncBackoff = 1500, vncTimer = null;
function openVnc() {
  if (ended || !RFB) return;
  clearTimeout(vncTimer);
  viewSize();
  // Each picture draws into its own holder and an older one goes at once: a picture whose
  // socket died silently disconnects only after noVNC's timeout, and until then its stale
  // canvas would stay first in the screen (casa-test F24: the picture stayed frozen).
  for (const old of [...screenEl.children]) old.remove();
  const holder = document.createElement('div'); holder.className = 'rfb'; screenEl.appendChild(holder);
  const r = new RFB(holder, wsUrl('websockify'), {shared: true, wsProtocols: ['binary']});
  rfb = r;
  // noVNC 1.6 measures its container with getBoundingClientRect, which includes our scale
  // transform: give it the untransformed size (pinned to Debian's noVNC 1.6.0).
  r._screenSize = function () { return {w: this._screen.clientWidth, h: this._screen.clientHeight}; };
  r.viewOnly = true; r.focusOnClick = false; r.background = '#111'; r.qualityLevel = 7;
  r.scaleViewport = false; r.clipViewport = true; r.resizeSession = false;
  r.addEventListener('connect', () => {
    if (rfb !== r) return;
    stats.vnc++; rfbConnected = true; vncBackoff = 1500;
    panTo(0, CROP ? toolbarH() : 0);   // the first picture is already below Chrome's toolbar
    refreshStatus(); setTimeout(() => fit(true), 300);
  });
  r.addEventListener('disconnect', () => {
    if (rfb !== r) return;
    rfbConnected = false; rfb = null; refreshStatus();
    if (ended) return;
    const mine = attempt;
    vncTimer = setTimeout(async () => { if (await askState() && mine === attempt && rfb === null) openVnc(); }, vncBackoff);
    vncBackoff = Math.min(8000, vncBackoff * 2);
  });
}
function closeVnc() { const r = rfb; rfb = null; rfbConnected = false; if (r) { try { r.disconnect(); } catch (err) {} } }
function closeCtl() { const w = ctl; ctl = null; if (w) { try { w.close(); } catch (err) {} } }

// Back from the background: both sockets may be dead without having said so. Reopen both.
// Attempts never overlap: only the latest one opens sockets, and pending retries are dropped.
let attempt = 0;
function reconnectAll() {
  if (ended) return;
  const mine = ++attempt;
  stats.reconnects++;
  setStatus(T('js.reconnecting', 'Reconnecting…'));
  clearTimeout(ctlTimer); clearTimeout(vncTimer);
  closeCtl(); closeVnc();
  ctlBackoff = 1000; vncBackoff = 1500;
  askState().then(ok => { if (ok && mine === attempt && !ended) { openCtl(); openVnc(); } });
}
let hiddenAt = 0;
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') hiddenAt = Date.now();
  else { reconnectAll(); kiosk(); }
});
addEventListener('pageshow', ev => { if (ev.persisted || hiddenAt) reconnectAll(); });
addEventListener('online', reconnectAll);
setInterval(() => {   // the controller pings every 5 s: silence means a dead socket
  if (!ended && ctl && ctl.readyState === 1 && Date.now() - lastBeat > BEAT_MS) reconnectAll();
}, 2000);

// ---------------------------------------------------------------- geometry
const disp = () => rfb && rfb._display;            // noVNC 1.6 private: viewport position
const canvas = () => { const h = screenEl.lastElementChild; return h ? h.querySelector('canvas') : null; };
function vp() { const d = disp(); return d ? d._viewportLoc : {x: 0, y: 0, w: 0, h: 0}; }
function geom() {
  const c = canvas(); if (!c || !c.width) return null;
  const r = c.getBoundingClientRect(); const v = vp();
  return {s: r.width / c.width, left: r.left, top: r.top, vx: v.x, vy: v.y};
}
const toClient = (X, Y, g = geom()) => g && [g.left + (X - g.vx) * g.s, g.top + (Y - g.vy) * g.s];
const toRemote = (x, y, g = geom()) => g && [Math.round(g.vx + (x - g.left) / g.s), Math.round(g.vy + (y - g.top) / g.s)];
function panTo(x, y) { const d = disp(); if (!d) return; const v = vp(); d.viewportChangePos(x - v.x, y - v.y); }
const barH = () => $('#pill').offsetHeight || 36;
function toolbarH() { return state ? Math.max(0, state.origin[1] - state.win[1]) : 0; }
// What part of this page the operator can see, in its own coordinates. In Home Assistant's
// frame (same origin) that is the frame's box within the top page's visual viewport, which the
// keyboard shrinks and scrolls; opened directly, this page's own visual viewport.
function topViewport() {
  try {
    const fe = window.frameElement, pv = fe && parent.visualViewport;
    if (!pv) return null;
    const r = fe.getBoundingClientRect(), cs = parent.getComputedStyle(fe);
    return {pv, top: r.top + fe.clientTop + (parseFloat(cs.paddingTop) || 0)};
  } catch (err) { return null; }   // another origin: this page's own viewport
}
function visibleBox() {
  const f = topViewport(), vv = window.visualViewport;
  let top = 0, bottom = innerHeight;
  if (f) { top = f.pv.offsetTop - f.top; bottom = top + f.pv.height; }
  else if (vv) { top = vv.offsetTop; bottom = top + vv.height; }
  top = Math.max(0, Math.min(top, innerHeight)); bottom = Math.max(top, Math.min(innerHeight, bottom));
  if (bottom - top < 120) return {top: 0, height: innerHeight};   // nothing sensible: the whole page
  return {top: Math.round(top), height: Math.round(bottom - top)};
}
// The picture's own zoom (F19): a scale ≥ 1 and an offset, applied over the fit's scale.
const zoom = {z: 1, x: 0, y: 0};
let screenW = 0, screenH = 0;     // the picture's size on the page at zoom 1
function clampZoom() {
  zoom.z = Math.max(1, Math.min(4, zoom.z));
  zoom.x = Math.min(0, Math.max(screenW - screenW * zoom.z, zoom.x));
  zoom.y = Math.min(0, Math.max(screenH - screenH * zoom.z, zoom.y));
}
// While the phone's keyboard is up (prototype K, casa-test F41) nothing is refitted: Chrome's
// window keeps its size — shrinking it for the keyboard left rows of the X screen below Chrome's
// window (black) in the picture when the phone's own layout did not follow — and the picture
// slides up instead (kbPan) so the field being typed into sits in the visible part.
let kbBox = null, kbPan = 0;
function applyZoom(k) {
  clampZoom();
  screenEl.style.transform = `translate(${zoom.x}px, ${zoom.y + kbPan}px) scale(${k * zoom.z})`;
}
// The picture's zoom, by its buttons (− · Fit · +), around the centre of what is visible of it.
const ZOOMS = [1, 1.5, 2, 3, 4];
function zoomTo(z) {
  cancelGesture();                // a finger still on the picture taps or drags nothing after it (Astra t14s-d1)
  const vis = visibleBox(), top = $('#screen').getBoundingClientRect().top - zoom.y - kbPan;
  const cx = screenW / 2, cy = vis.top + (vis.height + barH() - (kbBox ? STRIP_H : 0)) / 2 - top - kbPan;
  const px = (cx - zoom.x) / zoom.z, py = (cy - zoom.y) / zoom.z;
  Object.assign(zoom, {z, x: cx - px * z, y: cy - py * z});
  applyZoom(scaleK); zoomControls();
  trace('zoom ' + z);
}
function fitPicture() {
  cancelGesture();
  Object.assign(zoom, {z: 1, x: 0, y: 0}); kbPan = 0; applyZoom(scaleK);
  if (sess && sess.mode === 'field') panForKeyboard();      // only the keyboard's pan comes back
  zoomControls(); trace('zoom fit');
}
function zoomControls() {
  const zoomed = zoom.z > 1.01;
  $('#zoomout').hidden = $('#zoomfit').hidden = !zoomed;
  $('#zoomin').disabled = zoom.z >= 4;
}
const STRIP_H = 47;              // the strip above the phone's keyboard: iOS's form bar's height (measured)
function placeStrip() {
  const st = $('#kbstrip');
  if (st.hidden) return;
  const vis = visibleBox();
  st.style.top = Math.max(0, vis.top + vis.height - STRIP_H - app.getBoundingClientRect().top) + 'px';
}
function panForKeyboard() {
  if (!kbBox) return;
  placeStrip();
  const vis = visibleBox();
  app.style.top = vis.top + 'px';          // the bar stays in sight; the picture keeps its size
  const f = sess && sess.mode === 'field' ? sess.f : null;
  if (!f) { if (kbPan) { kbPan = 0; applyZoom(scaleK); } return; }
  const c = toClient(f.x, f.y + f.h / 2);
  if (!c) return;
  const cy = c[1] - kbPan, top = vis.top + barH() + 16, bottom = vis.top + vis.height - STRIP_H - 16;
  let pan = 0;
  if (cy < top || cy > bottom) pan = Math.min(0, top + (bottom - top) * 0.3 - cy);
  // the picture's bottom never rises above the visible bottom (the strip floats over the picture);
  // only a field that would then sit under the strip lifts it up to the strip's top, no further
  const cv = canvas();
  if (cv) {
    const end = cv.getBoundingClientRect().bottom - kbPan, visEnd = vis.top + vis.height;
    let p = Math.max(pan, Math.min(0, visEnd - end));
    if (cy + p > bottom) p = Math.max(pan, Math.min(0, visEnd - STRIP_H - end));
    pan = p;
  }
  if (pan !== kbPan) { kbPan = Math.max(pan, -screenH); applyZoom(scaleK); trace(`pan ${Math.round(kbPan)} (field at ${Math.round(cy)}, visible ${Math.round(top)}–${Math.round(bottom)})`); }
}
let scaleK = 1;
function viewSize() {
  const box = kbBox || visibleBox();
  const h = box.height - barH();
  const tb = CROP ? toolbarH() : 0;
  const k = Math.min(1, innerWidth / MINW);
  const rh = Math.min(h / k, 1080 - tb);
  scaleK = k; screenW = innerWidth; screenH = rh * k;
  Object.assign(screenEl.style, {width: (innerWidth / k) + 'px', height: rh + 'px',
    transformOrigin: '0 0', right: 'auto', bottom: 'auto'});
  applyZoom(k);
  return [Math.round(innerWidth / k), Math.round(rh), box, tb];
}
function place(box) { app.style.top = box.top + 'px'; app.style.setProperty('--vh', box.height + 'px'); }
let lastFit = '';
function fit(force, q) {
  const [w, h, box, tb] = viewSize();
  place(box);
  const key = `${w}x${h}+${tb}`;
  if (!force && key === lastFit) return;
  lastFit = key;
  // noVNC re-reads its size when the clip is toggled — and puts its viewport back at (0, 0), so
  // only a fit that re-pans below Chrome's toolbar toggles it (casa-test F36: the typing box
  // opening or closing made the picture jump by the toolbar's height). The pan follows in the
  // same task, so no picture at (0, 0) is ever drawn or tapped: a tap aimed at the picture
  // once landed a toolbar's height off when a later pan moved it before the finger came down.
  if (rfb) { rfb.clipViewport = !rfb.clipViewport; rfb.clipViewport = !rfb.clipViewport; }
  panTo(0, CROP ? tb : 0);
  send(q === undefined ? {t: 'fit', w, h: h + tb} : {t: 'fit', w, h: h + tb, q});
  // A field the operator is typing into stays in sight once the keyboard took its room: the
  // controller scrolls the remote focused element into view (casa-test F6).
  if (sess && sess.mode === 'field' && document.activeElement === kb) setTimeout(() => send({t: 'reveal'}), 350);
}
let fitT;
// Every viewport change — the keyboard, a rotation, Home Assistant's header coming back —
// places the viewer in what is visible at once, refits it shortly after and asks Home
// Assistant again to hide its header (F29: after the keyboard closed the header was back).
function viewportChanged() {
  viewportTrace('change'); kiosk();
  if (kbBox) { panForKeyboard(); return; }        // the keyboard is up: no refit, a pan
  place(visibleBox());
  clearTimeout(fitT); fitT = setTimeout(() => fit(false), 180);
}
addEventListener('resize', viewportChanged);
if (window.visualViewport) {
  visualViewport.addEventListener('resize', viewportChanged);
  visualViewport.addEventListener('scroll', viewportChanged);
}
{ const f = topViewport();
  if (f) { f.pv.addEventListener('resize', viewportChanged); f.pv.addEventListener('scroll', viewportChanged); } }

// ---------------------------------------------------------------- state from the controller
function onState(m) {
  const first = !state, prevTb = state ? toolbarH() : null;
  state = m;
  const host = /^[\w.-]+(:\d+)?$/.test(m.host) && m.host !== 'chromewebdata' ? m.host : 'Chrome';
  $('#host').textContent = CROP ? host + '  ▾' : T('js.hide_toolbar', 'hide toolbar ▴');
  if (first || prevTb !== toolbarH()) fit(true);
  if (first) screenEl.style.visibility = '';      // cropped by the fit above
  // A state probed before the controller took the tap says nothing about where it went.
  if (want && typeof m.ack === 'number' && m.ack >= want.q) want = null;
  kbState(m);
}

// ---------------------------------------------------------------- the field a tap names
// The smallest field of the last state that holds the remote point.
function fieldAt(rx, ry) {
  if (!state) return null;
  let best = null;
  for (const f of state.fields)
    if (rx >= f.x && rx <= f.x + f.w && ry >= f.y && ry <= f.y + f.h && (!best || f.w * f.h < best.w * best.h)) best = f;
  return best;
}
const keyOf = f => ({fi: f.fi, doc: f.doc, id: f.id});

// ---------------------------------------------------------------- touch: tap, swipe, hold-drag
// One finger only (operator ruling 2026-10-26). A second finger cancels the gesture (a drag gets its
// up) and nothing more happens until every finger lifted: the picture's zoom is its − · Fit · +
// buttons. Zoomed, a swipe pans the picture and, at its edge, carries on scrolling Chrome's page.
let g0 = null, multi = false; const ptrs = new Map();
function cancelGesture() {
  if (g0 && g0.mode === 'drag') send({t: 'up', x: g0.rx, y: g0.ry});
  if (g0) clearTimeout(g0.timer);
  g0 = null; stopFling();
}
touch.addEventListener('pointerdown', ev => {
  ev.preventDefault();
  ptrs.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
  if (ptrs.size > 1) { if (!multi) trace('two fingers: gesture cancelled'); multi = true; cancelGesture(); return; }
  if (multi || !state) return;                  // no picture yet (see screenEl's visibility)
  const [rx, ry] = toRemote(ev.clientX, ev.clientY) || [0, 0];
  stopFling();
  g0 = {x0: ev.clientX, y0: ev.clientY, lx: ev.clientX, ly: ev.clientY, rx, ry, mode: null, acc: 0, hist: [], scrolled: false};
  g0.timer = setTimeout(() => { if (g0 && !g0.mode) { g0.mode = 'drag'; send({t: 'down', x: rx, y: ry}); if (navigator.vibrate) navigator.vibrate(15); } }, 550);
});
touch.addEventListener('pointermove', ev => {
  if (ptrs.has(ev.pointerId)) ptrs.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
  if (!g0 || multi || ptrs.size > 1) return;
  const dx = ev.clientX - g0.lx, dy = ev.clientY - g0.ly; g0.lx = ev.clientX; g0.ly = ev.clientY;
  g0.hist.push([ev.timeStamp, ev.clientY]); while (g0.hist.length > 2 && ev.timeStamp - g0.hist[0][0] > 100) g0.hist.shift();
  if (!g0.mode && Math.hypot(ev.clientX - g0.x0, ev.clientY - g0.y0) > 10) { g0.mode = 'scroll'; clearTimeout(g0.timer); }
  const g = geom(); if (!g) return;
  if (g0.mode === 'scroll') {
    let rest = dy;
    if (zoom.z > 1.01) {                  // zoomed: the picture pans first
      const bx = zoom.x, by = zoom.y;
      zoom.x += dx; zoom.y += dy; clampZoom();
      rest = dy - (zoom.y - by);
      if (zoom.x !== bx || zoom.y !== by) applyZoom(scaleK);
    }
    g0.acc -= rest; const step = PPN * g.s;
    const n = Math.trunc(g0.acc / step); if (n) { g0.acc -= n * step; g0.scrolled = true; send({t: 'scroll', x: g0.rx, y: g0.ry, n}); }
  } else if (g0.mode === 'drag') {
    const [rx, ry] = toRemote(ev.clientX, ev.clientY, g); send({t: 'move', x: rx, y: ry}); g0.rx = rx; g0.ry = ry;
  }
});
// A tap on Chrome's address bar (its toolbar shown): the typing bar opens with it, inside the
// tap itself — a phone shows its keyboard only for a focus a touch makes (casa-test F13).
// Chrome's own UI is not a page and tells the page nothing, so the place decides: the toolbar
// row above the page, between the back/reload buttons and the icons on the right (measured
// at Chrome's 500-px window: back and reload end at 84 px, the star at 140 px from the right).
function onAddressBar(rx, ry) {
  if (CROP || !state) return false;
  const [wx, , ww] = state.win, oy = state.origin[1];
  return ry >= oy - 44 && ry < oy - 2 && rx >= wx + 86 && rx <= wx + ww - 142;
}
function endPtr(ev) {
  ptrs.delete(ev.pointerId);
  if (multi) { if (!ptrs.size) multi = false; return; }
  if (!g0) return; clearTimeout(g0.timer);
  if (!g0.mode) onTap(g0.rx, g0.ry); else if (g0.mode === 'drag') send({t: 'up', x: g0.rx, y: g0.ry});
  else if (g0.mode === 'scroll' && g0.hist.length > 1 && (g0.scrolled || zoom.z <= 1.01)) fling(g0);
  g0 = null;
}
let flingRaf = null;
function stopFling() { if (flingRaf) cancelAnimationFrame(flingRaf); flingRaf = null; }
function fling(g) {
  const [t0, y0] = g.hist[0], [t1, y1] = g.hist[g.hist.length - 1];
  let v = (y1 - y0) / Math.max(1, t1 - t0) * 16;
  if (Math.abs(v) < 8) return;
  let acc = g.acc; const {rx, ry} = g;
  const step = () => {
    const gg = geom(); if (!gg) return;
    acc -= v; v *= 0.94;
    const n = Math.trunc(acc / (PPN * gg.s)); if (n) { acc -= n * PPN * gg.s; send({t: 'scroll', x: rx, y: ry, n}); }
    flingRaf = Math.abs(v) > 1 ? requestAnimationFrame(step) : null;
  };
  flingRaf = requestAnimationFrame(step);
}
touch.addEventListener('pointerup', endPtr);
touch.addEventListener('pointercancel', ev => {
  ptrs.delete(ev.pointerId); if (!ptrs.size) multi = false;
  if (g0 && g0.mode === 'drag') send({t: 'up', x: g0.rx, y: g0.ry}); g0 = null;
});
// two fingers anywhere: iOS gets no pinch to start (the page itself must never zoom)
document.addEventListener('touchstart', ev => { if (ev.touches.length > 1) ev.preventDefault(); }, {passive: false});
touch.addEventListener('contextmenu', ev => ev.preventDefault());
// The page never scrolls or zooms under the operator's fingers (F19): iOS's own gestures too.
for (const type of ['touchmove', 'gesturestart', 'gesturechange', 'gestureend'])
  document.addEventListener(type, ev => {
    if (!(ev.target.closest && ev.target.closest('#dbg'))) ev.preventDefault();
  }, {passive: false});
// A tap on the picture is the viewer's alone: no synthetic click moves the focus off the typing
// box the tap may just have opened (F13). The box is focused again here: iOS raises its keyboard
// only for a focus inside a user gesture, which touchend surely is.
touch.addEventListener('touchend', ev => {
  ev.preventDefault();
  if (openedInTouch) { openedInTouch = false; if (sess && document.activeElement !== kb) kb.focus({preventScroll: true}); }
}, {passive: false});

// ---------------------------------------------------------------- the keyboard (live view K)
// The phone is Chrome's keyboard. A tap is a click in Chrome. When it lands on a field the last
// state listed (or on Chrome's address bar), one hidden input (#kb: in the bar at the top, never
// over the picture, so iOS has nothing to scroll into view) takes the focus inside the touch and
// the phone raises its keyboard. What is typed goes to Chrome as it is typed, so the field fills
// in the picture.
// A tap on a field opens an editing session bound to that field (its key). A state showing
// Chrome's focus on that very field confirms it — else nothing is sent, never to another field,
// and a line says so. Every batch names the field (`to`); the controller types it only while
// Chrome's focus is that element. The phone is a plain keyboard (operator ruling after K d3): each
// key event is one key in Chrome, mapped by its inputType, and Chrome decides what it does — no
// copy of the field is kept here.
// Return in a field closes the keyboard (the page's own button submits); in the address bar it
// goes (Enter).
const kb = $('#kb');
const TAP_WAIT_MS = 1500;   // how long a field tap waits for Chrome's focus to show on it
let seq = Date.now();    // message numbers (q), rising across reloads of this page
let want = null;         // {q}: the tap whose state is awaited
let sess = null;         // {mode: 'field'|'bar', f, key, ok: null|true|false, held: [actions]}
let openedInTouch = false, lastOutcome = 0, lastAck = 0, pasting = false, composing = false;
let composedIn = null;     // the session a composition began in: its text goes there or nowhere
const unresolved = new Set();     // batches sent whose outcome no state told yet
const sameKey = (a, b) => !!a && !!b && a.fi === b.fi && a.doc === b.doc && a.id === b.id;
function focused() { return state ? state.fields.find(f => f.focused) || null : null; }
function hints() {
  const f = sess && sess.f, pw = !!f && (f.pw || f.type === 'password');
  const type = pw ? 'password' : 'text';
  if (kb.type !== type) kb.type = type;
  const im = sess && sess.mode === 'bar' ? 'url'
    : (f && !pw ? (f.im || ({email: 'email', tel: 'tel', number: 'numeric', url: 'url'})[f.type] || '') : '');
  if (im) kb.inputMode = im; else kb.removeAttribute('inputmode');
  kb.autocomplete = f && f.ac === 'one-time-code' ? 'one-time-code' : 'off';
  kb.enterKeyHint = sess && sess.mode === 'bar' ? 'go' : 'done';
}
const ctlOpen = () => !!(ctl && ctl.readyState === 1);
function kbUp(mode, f) {          // inside the touch: the keyboard comes up at once
  endSession();                   // a previous session ends here (the tap flushed what it could)
  sess = {mode, f, key: f ? keyOf(f) : null, ok: mode === 'bar' ? true : null, held: [], closing: false};
  composing = false;              // a composition the old session left is over (Astra K r3)
  if (!kbBox) kbBox = visibleBox();       // the layout before the keyboard: it stays
  if (mode === 'field') {
    const mine = sess;
    sess.timer = setTimeout(() => {         // the tap's time: no confirmation, no session
      if (sess === mine && sess.ok === null) kbDown(T('js.retap', 'Tap missed — tap the field again'));
    }, TAP_WAIT_MS);
  }
  hints();
  kb.focus({preventScroll: true}); reset();
  openedInTouch = true;
  $('#pastebtn').hidden = !(navigator.clipboard && navigator.clipboard.readText);
  $('#kbstrip').hidden = false; setHasText(mode === 'bar' || !!(f && f.tx)); placeStrip();
  trace('session ' + mode + (f ? ' ' + (f.lb || f.type) : ''));
}
// A session ends; keys typed into it that never went out (or a composition cut off) are
// reported, never kept for later.
function endSession(why) {
  const lost = !!sess && (sess.held.length > 0 || (composing && kb.value.replace(/\u200b/g, '') !== ''));
  sess = null; want = null;
  if (why || lost) toast(why || T('js.dropped', 'Not sent — tap the field, type again'));
}
function kbDown(why) {
  endSession(why);
  if (kbBox) { kbBox = null; kbPan = 0; applyZoom(scaleK); setTimeout(viewportChanged, 0); }
  if (document.activeElement === kb) kb.blur();
  kb.value = ''; $('#kbstrip').hidden = true; composing = false;
}
// #kb holds two zero-width spaces with the caret between them, so Backspace and Delete always have
// something to delete and keep firing; every key is cancelled and goes out as that key.
const SENTINEL = '​​';
function reset() {
  if (kb.value !== SENTINEL) kb.value = SENTINEL;
  try { if (kb.selectionStart !== 1 || kb.selectionEnd !== 1) kb.setSelectionRange(1, 1); } catch (err) { /* no selection */ }
}
// One phone key event, one action — by its inputType, mechanically; anything else sends nothing.
const TYPED = new Set(['insertText', 'insertFromPaste', 'insertFromDictation']);
const KEY_OF = {deleteContentBackward: 'Backspace', deleteWordBackward: 'CtrlBackspace', deleteContentForward: 'Delete'};
const COMPOSED = new Set(['insertCompositionText', 'deleteCompositionText', 'insertFromComposition', 'deleteByComposition']);
function act(a) {                 // queued for the session's field, in order; out once it may
  if (!sess) return;
  sess.held.push(a); flush();
}
// The strip's arrows only while the field holds text: the probe's has-text (`tx`) at the session's
// start, on after any text typed or pasted, off again only when a fresh probe (one that
// acknowledges every key sent) says the field is empty.
let lastSentQ = 0;
function setHasText(on) {
  if (!sess) return;
  if (sess.hasText !== on) trace('arrows ' + (on ? 'on' : 'off'));
  sess.hasText = on;
  $('#caretleft').hidden = $('#caretright').hidden = !on;
}
function typeText(s) {
  if (!s) return;
  if (/[\r\n]/.test(s)) { toast(T('js.multiline', 'Not pasted: more than one line')); return; }   // a line break would be Return
  const chars = Array.from(s);
  for (let i = 0; i < chars.length; i += MAX_TEXT) act({t: 'text', s: chars.slice(i, i + MAX_TEXT).join('')});
}
function flush() {               // what the session holds goes out, once it may
  if (!sess || sess.ok !== true || !ctlOpen()) return;     // kept until then, or reported
  const bar = sess.mode === 'bar';
  while (sess.held.length) {
    const a = sess.held.shift();
    // `ok`: every outcome up to the last state's ack has been read here (the controller forgets them)
    const out = b => { const q = ++seq; unresolved.add(q); lastSentQ = q; if (b.t === 'text') setHasText(true); send({...b, q, ok: lastAck, ...(bar ? {bar: true} : {to: sess.key})}); };
    // the address bar: its first keys replace the address (a click there leaves the caret at its end)
    if (bar && !sess.replaced && (a.k === 'Left' || a.k === 'Right')) sess.replaced = true;   // editing the address
    if (bar && !sess.replaced && a.k !== 'Enter') { sess.replaced = true; out({t: 'key', k: 'SelectAll'}); }
    out(a);
  }
}
function kbState(m) {            // a state arrived
  // The outcome of every batch travels in the states: refused (not typed) or unsure (typing
  // failed part-way) ends the session with a line; nothing is re-sent.
  const fresh = list => (Array.isArray(list) ? list : []).filter(q => q > lastOutcome);
  const refused = fresh(m.refused), unsure = fresh(m.unsure);
  if (typeof m.ack === 'number') {
    lastAck = Math.max(lastAck, m.ack);
    for (const q of [...unresolved]) if (q <= m.ack) unresolved.delete(q);
  }
  if (refused.length || unsure.length) {
    // told whether or not the session is still open (Return may have closed it — Astra K d2)
    lastOutcome = Math.max(lastOutcome, ...refused, ...unsure);
    const why = refused.length ? T('js.unsent', 'Not sent: the focus moved')
      : T('js.unsure', 'Not sure it was sent — check the field');
    if (sess) kbDown(why); else toast(why);
    return;
  }
  if (!sess || sess.mode !== 'field' || want) return;
  const f = focused();
  if (sess.ok === null) {
    // the tap's answer: Chrome's focus must be the field the operator tapped. Chrome handles
    // the click a moment after it was sent, so a state still showing the old focus is not yet
    // a miss: the session waits (keys held) until the tapped field shows focused, or the
    // tap's time runs out (sess.timer: then always "Tap missed") — never another field.
    if (f && sameKey(keyOf(f), sess.key)) {
      clearTimeout(sess.timer);
      sess.ok = true; sess.f = f; hints(); trace('confirmed'); flush();
      if (sess.closing) kbDown(); else panForKeyboard();
    }
    return;
  }
  if (!f) kbDown();                                   // nothing editable any more
  else if (!sameKey(keyOf(f), sess.key)) kbDown(T('js.unsent', 'Not sent: the focus moved'));
  else {
    sess.f = f; hints(); panForKeyboard();
    if (typeof f.tx === 'boolean' && typeof m.ack === 'number' && m.ack >= lastSentQ && !sess.held.length) setHasText(f.tx);
  }
}
// A tap on the picture: a click in Chrome; on a field or the address bar the keyboard comes up.
function onTap(rx, ry) {
  if (sess) flush();
  const q = ++seq;
  const bar = onAddressBar(rx, ry), f = bar ? null : fieldAt(rx, ry);
  send({t: 'tap', x: rx, y: ry, q});
  if (bar) kbUp('bar', null);
  else if (f) kbUp('field', f);
  else if (sess) kbDown();
  want = {q};                     // after the session's start: the tap's answer is awaited
}
// Return: in a page field it closes the keyboard (the page's own button submits); in the address
// bar it goes (Enter).
function ret() {
  if (!sess) return;
  if (sess.mode === 'bar') { act({t: 'key', k: 'Enter'}); kbDown(); }
  else if (sess.ok === true) kbDown();
  else { sess.closing = true; kb.blur(); }   // the keyboard goes; the keys go once confirmed
}
kb.addEventListener('beforeinput', ev => {
  const it = ev.inputType;
  trace('beforeinput ' + it + (kb.type === 'password' ? '' : ' ' + JSON.stringify(ev.data)) + (ev.isComposing ? ' (composing)' : ''));
  if (ev.isComposing || COMPOSED.has(it)) return;   // a composition's text goes at its end
  ev.preventDefault();
  if (!sess) return;
  const typed = TYPED.has(it) ? (ev.data != null ? ev.data : (ev.dataTransfer ? ev.dataTransfer.getData('text/plain') : '')) : '';
  // a paste or suggestion with no text (an image) is refused with the unsupported line, never guessed
  if (TYPED.has(it) && !typed) toast(T('js.unsupported', "That key isn't supported here"));
  else if (TYPED.has(it)) typeText(typed);
  else if (KEY_OF[it]) act({t: 'key', k: KEY_OF[it]});
  else if (it === 'insertLineBreak' || it === 'insertParagraph') ret();
  else toast(T('js.unsupported', "That key isn't supported here"));
});
kb.addEventListener('input', ev => {           // only for an event that could not be cancelled
  trace('input ' + ev.inputType);
  if (!ev.isComposing && !composing) reset();
});
kb.addEventListener('compositionstart', () => { composing = true; composedIn = sess; trace('compositionstart'); });
kb.addEventListener('compositionend', ev => {
  composing = false; trace('compositionend');
  // a composition that outlived its session (another tap; reported then) goes nowhere — like a
  // paste, it belongs to the session it began in (Terra K r7)
  if (sess && sess === composedIn) typeText(ev.data);
  composedIn = null; reset();
});
// the caret stays between the two sentinels (a drag on the space bar may move it)
document.addEventListener('selectionchange', () => { if (document.activeElement === kb && !composing) reset(); });
kb.addEventListener('keydown', ev => {
  // in a password field only named keys (Enter, Backspace, Shift…) are shown (Astra K r1)
  trace('keydown ' + (kb.type === 'password' && !/^[A-Z][A-Za-z0-9]+$/.test(ev.key) ? '•' : ev.key) + (ev.isComposing ? ' (composing)' : ''));
  if (sess && ev.key === 'Enter' && !ev.isComposing) { ev.preventDefault(); ret(); }
});
kb.addEventListener('blur', () => setTimeout(() => {
  if (document.activeElement !== kb && sess && !pasting && !sess.closing) { flush(); kbDown(); }
}, 250));
// Paste (casa-test F46): one read at a time — the strip's Paste is disabled until it settles (iOS shows
// its own Paste callout, and a late click must not start a second read) — and the text goes in
// exactly once, to the session that asked, at once, with a line saying so. The session stays open
// while iOS's callout has the focus.
let pasteSeq = 0;
async function paste() {
  // A pending paste keeps its session (iOS's Allow Paste prompt and Paste bubble take the focus,
  // and may send the page to the background): the text goes to that session's field, bound and
  // guarded — never to another session.
  const s = sess, btn = $('#pastebtn');
  if (!s || pasting) return;
  const hold = ++pasteSeq;
  pasting = true; s.pasteHold = hold; btn.disabled = true;
  trace('paste asked' + (document.activeElement === kb ? '' : ' (keyboard not focused)'));
  try {
    let text = await navigator.clipboard.readText();
    trace('paste ' + (text ? Array.from(text).length + ' chars' : 'nothing') + (document.activeElement === kb ? ', keyboard focused' : ', keyboard not focused'));
    text = (text || '').replace(/\r?\n$/, '');             // a copied code's trailing line break
    if (!text) { toast(T('js.unsupported', "That key isn't supported here")); return; }   // an image, nothing
    if (/[\r\n]/.test(text)) { toast(T('js.multiline', 'Not pasted: more than one line')); return; }
    if (sess !== s) { toast(T('js.not_pasted', 'Not pasted: the field changed')); return; }   // never to another field
    typeText(text); setHasText(true);
    toast(T('js.pasted', 'Pasted {n} characters').replace('{n}', String(Array.from(text).length)));
  } catch (err) {                 // iOS's prompt denied, or no clipboard access: said, never silent (Terra t14s-d1)
    trace('paste refused ' + err.name);
    toast(T('js.paste_refused', 'Not pasted: no access to the clipboard'));
  }
  finally {
    pasting = false; btn.disabled = false;
    setTimeout(() => { if (s.pasteHold === hold) s.pasteHold = 0; }, 2000);   // a later paste keeps its own
    // the keyboard back if iOS took it: a focus outside a touch may or may not raise it
    if (sess === s && document.activeElement !== kb) {
      kb.focus({preventScroll: true});
      setTimeout(() => {
        if (sess !== s) return;
        if (document.activeElement === kb) trace('paste: the keyboard input focused again');
        else { trace('paste: keyboard gone (iOS took the focus)'); flush(); kbDown(); }
      }, 300);
    }
  }
}
{
  // one gesture, one read: the click that follows a pointerup is the same gesture (Astra K d2);
  // a click without a pointerup is a keyboard activation
  const btn = $('#pastebtn');
  let tapped = 0;
  btn.addEventListener('pointerdown', ev => ev.preventDefault());
  btn.addEventListener('pointerup', () => { tapped = Date.now(); paste(); });
  btn.addEventListener('click', () => { if (Date.now() - tapped > 1500) paste(); });
}
// ---------------------------------------------------------------- debug trace (prototype)
// A long press on the bar shows what the phone's keyboard and viewport actually do (input
// events, never a password's characters; the viewport's numbers).
const dbg = $('#dbg'), lines = [];
function trace(line) {
  lines.push(new Date().toISOString().slice(17, 23) + ' ' + line);
  while (lines.length > 60) lines.shift();
  if (!dbg.hidden) dbg.textContent = lines.slice().reverse().join('\n');
}
function viewportTrace(why) {
  try {
    const f = topViewport(), c = canvas(), r = c ? c.getBoundingClientRect() : null, v = vp();
    const a = app.getBoundingClientRect(), sc = screenEl.getBoundingClientRect();
    trace(`vp ${why}: inner ${innerWidth}x${innerHeight} own-vv ${window.visualViewport ? Math.round(visualViewport.height) + '@' + Math.round(visualViewport.offsetTop) : '-'}`
      + (f ? ` top-vv ${Math.round(f.pv.height)}@${Math.round(f.pv.offsetTop)} frame-top ${Math.round(f.top)} top-scroll ${Math.round(parent.scrollY)}` : ' no-top')
      + ` app ${Math.round(a.top)}+${Math.round(a.height)} screen ${Math.round(sc.top)}+${Math.round(sc.height)}`
      + (c ? ` canvas ${c.width}x${c.height} @${Math.round(r.top)}+${Math.round(r.height)} vnc-vp ${v.x},${v.y} ${v.w}x${v.h}` : ' no-canvas'));
  } catch (err) { trace('vp ' + why + ' ' + err.name); }
}
{
  let hold = null;
  $('#pill').addEventListener('pointerdown', () => { hold = setTimeout(() => {
    dbg.hidden = !dbg.hidden; viewportTrace('shown'); trace('debug ' + (dbg.hidden ? 'off' : 'on'));
    if (!dbg.hidden) dbg.textContent = lines.slice().reverse().join('\n'); }, 800); });
  for (const t of ['pointerup', 'pointercancel', 'pointerleave']) $('#pill').addEventListener(t, () => clearTimeout(hold));
}

// ---------------------------------------------------------------- the strip and the zoom controls
// A button that keeps the keyboard: its press never takes the focus from #kb; it acts on pointerup
// (one gesture, one action); a click without a pointerup is a keyboard activation.
function keepsKeyboard(btn, fn) {
  let tapped = 0;
  btn.addEventListener('pointerdown', ev => ev.preventDefault());
  btn.addEventListener('mousedown', ev => ev.preventDefault());           // the focus stays on #kb
  btn.addEventListener('touchend', ev => ev.preventDefault(), {passive: false});
  btn.addEventListener('pointerup', () => { tapped = Date.now(); fn(); });
  btn.addEventListener('click', () => { if (Date.now() - tapped > 1500) fn(); });
}
keepsKeyboard($('#caretleft'), () => { if (sess) { act({t: 'key', k: 'Left'}); trace('strip ←'); } });
keepsKeyboard($('#caretright'), () => { if (sess) { act({t: 'key', k: 'Right'}); trace('strip →'); } });
keepsKeyboard($('#zoomin'), () => { const z = ZOOMS.find(v => v > zoom.z + 0.01); if (z) zoomTo(z); });
keepsKeyboard($('#zoomout'), () => { const z = [...ZOOMS].reverse().find(v => v < zoom.z - 0.01); if (z) zoomTo(z); });
keepsKeyboard($('#zoomfit'), fitPicture);

// ---------------------------------------------------------------- remote -> phone copy
let toastT;
function toast(msg, onTap) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false; t.onclick = onTap || null;
  $('#pill').classList.add('toasting');    // the toast shares the bar with the site name (F18)
  // a line wider than the pill wraps to two smaller lines (F37), measured, not guessed (F38)
  t.classList.remove('two');
  if (!onTap && t.scrollWidth > t.clientWidth + 1) t.classList.add('two');
  clearTimeout(toastT);
  toastT = setTimeout(() => { t.hidden = true; $('#pill').classList.remove('toasting'); }, onTap ? 9000 : 2500);
}
function onSel(text) {
  toast(T('js.copy', 'Copy “{text}”').replace('{text}', () => (text.length > 32 ? text.slice(0, 32) + '…' : text)), async () => {
    try { await navigator.clipboard.writeText(text); }
    catch (err) { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
    toast(T('js.copied', 'Copied'));
  });
}

// ---------------------------------------------------------------- Home Assistant's header
const KIOSK = window.parent !== window;
let unkioskSent = false;
// Asked again at every viewport change: Home Assistant enables its kiosk mode only when it is
// off, so a repeat costs nothing, and it brings the header back hidden when something showed it.
function kiosk() {
  if (!KIOSK || ended || unkioskSent) return;
  stats.kiosk++;
  parent.postMessage({type: 'home-assistant/subscribe-properties', kioskMode: true, handleSafeArea: true}, '*');
}
if (KIOSK) {
  kiosk();
  addEventListener('message', ev => {
    if (ev.source === parent && ev.data && ev.data.type === 'home-assistant/properties' && ev.data.safeAreaInsets) {
      const s = ev.data.safeAreaInsets; for (const k of ['top', 'right', 'bottom', 'left']) app.style.setProperty('--sa-' + k, s[k] || '0px');
    }
  });
}
function unkiosk() { if (KIOSK && !unkioskSent) { unkioskSent = true; parent.postMessage({type: 'home-assistant/unsubscribe-properties'}, '*'); } }
addEventListener('pagehide', unkiosk);

// ---------------------------------------------------------------- Done
$('#doneform').addEventListener('submit', () => {
  ended = true; closeCtl(); closeVnc(); unkiosk(); setStatus(T('js.finishing', 'Finishing…'));
  Object.assign(zoom, {z: 1, x: 0, y: 0}); applyZoom(scaleK); zoomControls();   // the picture's zoom ends with it
});

// tap the site name to bring Chrome's own toolbar back (address bar, extension icons, prompts)
$('#host').addEventListener('click', () => { CROP = !CROP; if (!CROP) panTo(0, 0); fit(true); if (state) onState(state); });
setStatus(T('js.connecting', 'Connecting…'));
openCtl();
import(PREFIX + '/novnc/core/rfb.js').then(mod => { RFB = mod.default; openVnc(); })
  .catch(() => setStatus(T('js.no_picture', 'The picture could not load. Reload the page.')));
