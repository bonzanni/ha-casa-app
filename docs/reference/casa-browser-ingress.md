---
last_reviewed: 2026-11-13
---

# Casa Browser — ingress UI and the live view

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The operator's side of the optional `casa_browser/` app: the pages Home Assistant shows in the
app's panel (ingress), pairing, the sites, and the live view a run hands to the
operator — `casa_browser/rootfs/opt/casa_browser/ui.py` (the pages), `liveview.py` (grants,
claims, the viewer's sockets, the window, input, the field probe), the viewer's static files
under `static/`, the nginx configuration `rootfs/etc/nginx/casa_browser.conf` and the s6 units
`svc-browser-nginx` and `svc-browser-vnc`. The run states around a live view (its lease, the
broker's capture, Done's release) are the run controller's
([`casa-browser-runs.md`](casa-browser-runs.md)).

## Mental model

**Three processes, one door.** nginx (user `casa-ingress`, uid 1105) listens on port 8099 and
admits only Supervisor's ingress address — `allow <peer>; deny all;`, rendered at start by
`python -m casa_browser.ui ingress-peer` (a missing file stops nginx from starting). It serves
the app's own `static/` files and noVNC's `core/` and `vendor/` modules (Debian's 1.6.0 files,
nothing of noVNC's own app), and proxies everything else, WebSocket upgrades included, to the
UI's Unix socket `/run/casa-browser/ui/ui.sock` (directory `0750` controller:casa-ingress). The
UI runs inside the controller process (uid 1103). x11vnc (user `casa-vnc`, uid 1104) serves
display `:99` **view-only** on `/run/casa-browser/vnc/vnc.sock` (directory `0750`
casa-vnc:casa-vncview; `casa-vncview` holds only the controller), with no TCP listener
(`-rfbport 0 -rfbportv6 -1`) and no shared memory (`-noshm`: the X server is another user).
The network floor gives `casa-vnc` no connection at all and `casa-ingress` only replies on
port 8099 ([`casa-browser-runtime.md`](casa-browser-runtime.md)). Each of the three readers of
the display — Chrome, x11vnc, the controller — has its own copy of the X cookie.

**Every URL from the prefix.** Supervisor strips the ingress prefix and sends `X-Ingress-Path`;
a request without one of the form `/api/hassio_ingress/<token>` is refused (403). The prefix is
the root of every link, form, redirect, asset and WebSocket URL a page carries.

**Every mutation is a form.** A POST needs a nonce the server issued for exactly that path
(random, single use, one hour, at most 4096 kept) and, when the browser sends `Origin`, the
forwarded origin (`X-Forwarded-Proto` + `X-Forwarded-Host`; without `Origin`,
`Sec-Fetch-Site` must be `same-origin` when present); otherwise 403 and nothing happens.
Home Assistant's ingress replaces the app's `Referrer-Policy` header with `no-referrer`, under
which a browser posts a form with `Origin: null`; so every page and the viewer carry
`<meta name="referrer" content="same-origin">` first in their head, which governs the forms
they submit. A
save tap's nonce is its `consent_id` for the broker. Pages are escaped, never cached
(`Cache-Control: no-store`), framed only by their own origin, and run only this app's scripts
(CSP; `img-src` admits `data:` for noVNC's JPEG rectangles; `Referrer-Policy: same-origin`). `operator_ref` is
`ingress:<X-Remote-User-Id>` (Supervisor sets that header itself), else `ingress:operator`.

## Pages

| Path | What |
|---|---|
| `/` | "Needs you" first — every run in `waiting_live`, then every run in `live` (to reconnect), each in its own card with one **Open**, then every run waiting on an open question that offers a live view (its questions wait in Telegram) — then recent runs as a list (state, today/yesterday or the date), notices folded |
| `/runs/{run}` | the run's state, task, files with their delivery outcome, the step log with its operator-only screenshots (`/runs/{run}/screenshots/{id}`: that run's ids only) |
| `/sites` | registered sites, one row each: how the site signs in, or "provider sign-in needs a live login" when the binding was learned under another proxy binding key than the current one — after an HA restore, which does not bring the key back — or under none recorded; **Add site** leads to `/sites/new` |
| `/sites/{site}` | one site: its sign-in, its provider binding ("bound to {provider}", "provider sign-in not bound", or the live-login line above), its addresses (one-tap copy), and **Forget sign-in**, which asks in a dialog before its form is sent (the controller's forget, never a vault call) |
| `/sites/new` | registration (posts to `/sites`; a 1Password item, or none: the first login live view's save creates it; the item's extra field names, never contents, are shown back on `/sites` with the whole-vault warning) |
| `/setup`, `/pair` | readiness (1Password, the effective language and time zone), the guest's state from the vault's users (read-only, can edit items, or unknown) with their counts — a warning, nothing to tick, when no vault user is read-only: "The browser's 1Password guest account can edit items. Casa Browser only needs to read them; consider making it read-only." (design §5.2; logins never wait on it) — pairing: "first caller within two minutes"; a window that closed unclaimed says that pairing needs the browsing specialist (Fido) installed in Casa and asked to pair; after a claim the caller's self-reported name (escaped, marked as reported by the caller) and the address the claim came from. Re-pairing (`Pairing.arm`) ends every run and live view of the old pairing |

**Look.** The pages take Home Assistant's look: its card, row, button and tab sizes and its Material Design icons (inline SVG), in `static/style.css` alone — the CSP admits no inline style, so no page carries a `style` attribute (`test_ui.py`). The colours are Home Assistant's theme values under their own names (`--primary-color`, `--card-background-color`, …): `static/theme.js`, loaded first in every page's head and the viewer's, reads the values Home Assistant's page resolved — the panel is a frame on Home Assistant's own origin — and sets them, again when that theme changes; it only reads. Opened outside that frame, the stylesheet's copies of Home Assistant's default light and dark values apply. The tabs (Home, Sites, Setup) sit at the bottom under 871 px, as Home Assistant's own settings pages do, and at the top above it; a sub-page (a run, a site, Add site, pairing) has a back arrow. A form that ends something asks first: `data-confirm` on the form, a dialog from `static/app.js`.

**Language.** Every string the pages and the viewer show comes from one catalog per language,
`locales/<language>.json` beside `i18n.py`: English is complete by definition, and the others
(Italian, Dutch, German) hold exactly English's keys with the same placeholders
(`test_i18n.py`). The language is the effective locale's — Home Assistant's, or the `language`
option ([`casa-browser.md`](casa-browser.md)) — reduced to its primary subtag; a language
without a catalog is shown in English, and `lang` names the language actually shown. A
template is plain text: the page code escapes it and inserts each value, escaped, itself. The
viewer gets its own strings as page placeholders and `live.js` its table through the
`data-strings` attribute. What a page shows from elsewhere stays as it came: a site's name, an
item's description (the model's words), the step log's and the notices' text (also the
assistant's data), an event's kind and outcome. The plain-text refusals of the boundary
middleware (before a page exists) are English. Home Assistant's option texts are
`casa_browser/translations/<language>.yaml`, one per catalog language.

## The live view

**Grants and claims.** `request_live` gets a grant from `LiveView.issue` (120 s, single use;
its path `live/{grant}` has a page with an Open form). An Open button says "Opening…" and
disables itself while the view opens (`static/app.js`), so a second tap cannot spend its form.
**Open on the home page**
(`open_from_home`) reconnects an established view — a new cookie, the old page no longer the
viewer — or claims the run's grant, expired or not; a grant's own page (`claim`) accepts only
that run's unexpired grant and never reconnects. Home shows a view that starts waiting by
itself: it reloads when `GET /home/stamp` (a hash of the waiting views and questions) changes,
checked every 3 s while visible, never while one of its forms is being sent (F40). Every claim spends its grant, whatever its
outcome, so a grant id is never accepted twice. Claims run under one lock, so two Opens never start two views. A claim calls
the controller's `begin_live(run, expose)`; `expose` creates the view's session, so a session —
and with it any socket or input — exists only once the run is `live` (the model paused) and, on
an account session, the manual lease is held and, for a login/assist view, the broker
acknowledged the capture (a public-session assist view has neither). A refused claim says the view could not
be offered (the run is back to `running`). The cookie is `HttpOnly`, `SameSite=Strict`, scoped
to the ingress prefix and `Secure` behind https.

**One viewer.** Every viewer page served with the cookie gets a fresh viewer id; the sockets
`view/{run}/ctl` and `view/{run}/websockify` carry it and are accepted only for the cookie's
session, its current viewer, the phase `viewing` and the run in `live`. Each input re-checks its
socket's viewer under the screen lock, so a page that is no longer the viewer (another tab, a
second Open, the page before a reload) can neither reconnect nor have a queued input injected:
a change of viewer ends the old page's view the way every end does (new id at once, its sockets
closed, then the screen lock drained), so the Open or reload returns only after the old page's
operation in flight finished. Its Done form is refused too: Done's nonce is bound to the page's
viewer id. A new socket always gets the current state at once.
`view/{run}/state` tells a page whether it is still the viewer.

**The screen lock.** Everything the view does to Chrome — the window fit, input, the probe, the
selection poll — runs under one lock and checks its session first; each call it makes is
bounded (5 s for the window, the probe and the selection, 10 s for an injection). Every end of
the view — Done, and the controller's cancel, interruption, re-pair, forget, shutdown and any
end of the run, before they wait for anything — sets the phase, closes both sockets and then
takes the lock, so the operation in flight finishes first and nothing runs after it; the
controller drains it before it releases the capture.

**The picture.** `/websockify` is a byte bridge from the WebSocket to x11vnc's socket, checked
before every frame: the moment its page is no longer the current viewer, it forwards nothing more.
noVNC runs `viewOnly`. Chrome's window — only the window; the X screen stays 1920×1080 — is fitted to the
viewer through CDP `Browser.setWindowBounds` (at least 500 px wide: a narrower phone scales the
picture by width/500; at most 1080 px tall: a tall phone is letterboxed). Chrome's toolbar is
cropped and replaced by the viewer's bar (site, Done); tapping the site shows the toolbar. The picture
shows, and takes taps, only from the first state on (it names the toolbar's height); the crop is applied
the moment the picture connects and again in the same step as every refit: the picture never shows the
toolbar for a moment and then jumps (a tap aimed just before the jump landed a toolbar's height off).
The viewer asks Home Assistant's panel to hide its header (`home-assistant/subscribe-properties`
with `kioskMode`) — again at every viewport change, since Home Assistant enables its kiosk mode
only when it is off — and undoes it at Done or when the page closes. It places itself in the part
of its frame the operator can see (inside Home Assistant's same-origin frame, the frame's box
within the top page's visual viewport) and refits Chrome — except while the keyboard is up (see
Typing). One finger: a tap clicks, a swipe scrolls Chrome, a hold then a drag drags; a second
finger cancels the gesture (a drag gets its up) until all lift. − · Fit · +, in the bar and so never
over a site's button, zoom the picture (1–4×; each first cancels a gesture), which taps follow; zoomed, a swipe pans it, then scrolls
Chrome at its edge. The page takes every touch (`touch-action: none`, touch, gesture and
two-touch `touchstart` defaults prevented), so it never zooms or scrolls itself; Done resets the
zoom. Each picture draws into its own holder and a reconnect removes the
old one at once.

**Input.** All of it arrives on `/ctl` as fixed operations — tap, scroll (wheel
notches, ≤ 40), drag, text (≤ 256 characters a message, never empty; the viewer sends a
long paste in pieces of whole characters), ten keys, fit, and reveal (bring the remote focused
element into view, following the focus into frames, open shadow roots included —
`liveview.reveal`; it carries nothing and reads nothing back) — clamped to
Chrome's window as last probed (nothing before the first probe), and is injected at the X level
with `xdotool` (an injection that outlives its 10 s bound is stopped, so nothing types on
after it). The one exception is text for a field that holds a character the display's US keymap
lacks (an accent, an emoji; `liveview.x_typable`): it goes whole, in order, through Chrome's own
input on that field's page — ASCII as US-keyboard keys, every other code point as a key down
carrying it and a key up, so a page's key handlers still run — because `xdotool` binds such a
character to a spare keycode for one press and a busy Chrome can read that binding after it
changed, losing the character or typing another. Anything else is dropped.

**Typing** (live view K). Nothing lies over the picture; a tap is
a click in Chrome. On a field of the last state (the smallest holding the point) or on Chrome's
address bar (toolbar shown; its place decides), a hidden input in the bar (`#kb`) takes the
focus in the same touch (`pointerup`, again in `touchend`: a phone raises its keyboard only for a
focus a touch makes). Other taps never raise it; a tap off the fields closes it. A field tap opens a **session
bound to that field's key** — frame index, document (`timeOrigin`) and an id the probe gives each
editable as an expando in the controller's isolated world, so a replaced field is a new id. Within
1.5 s a state acknowledging the tap (every state carries `ack`, the last message number taken,
read with the probe under the screen lock) must show Chrome's focus on that very element; then the session is confirmed and what was typed
meanwhile goes out. Otherwise — a dropdown or overlay took the click — nothing goes and "Tap
missed — tap the field again": keys are never redirected to another field. The phone is
Chrome's plain keyboard: each key event is cancelled and sent as one action by its `inputType` (typed,
pasted or dictated text; Backspace, Ctrl+Backspace, Delete; a line break is Return) and Chrome
decides what it does. Anything else sends
nothing: "That key isn't supported here". No copy of the field is kept; `#kb` holds two
zero-width spaces, caret between, so Backspace keeps firing. A composition goes once it ends. **No key is typed into a field the controller
has not just checked:** every batch names its field (`to`) and is typed only if, at that moment,
the field's page is the one shown — brought to the front first, so Chrome's keys go to it — and
its focus chain (each frame's element its parent's focused element) ends at that element;
otherwise nothing is typed and the states list the batch as refused (or, when the typing failed
part-way, as unsure) until the viewer has read that (`ok`); the viewer ends the session with "Not
sent: the focus moved" or "Not sure it was sent", also after the keyboard closed, and nothing is
re-sent. Keys that never went out (no field confirmed, another tap, a closed socket, a batch the
old socket never answered) are reported, never kept for later. Return in a
field closes the keyboard and sends no Enter (the page's own button submits); in the address bar
the first keys replace the address (select-all) and return goes — the one target nothing checks.
`#kb` is a password input for a field ever seen as one (or with a password
autocomplete), else carries inputmode and one-time-code hints; it asks the keyboard for no help (no
autocorrect, capitals, spelling, inline suggestions). While the keyboard is up Chrome is not refitted (F41): the layout stays, the bar follows the visible top, the picture slides so
the field sits above the keyboard.
A strip above the keyboard (`#kbstrip`) offers Paste and ← → without taking `#kb`'s focus. ← →
are one guarded Left/Right key each, shown while the field holds text (each field's `tx`, a
boolean); in the address bar a first arrow keeps the address. Paste reads the clipboard once per
gesture for the session that asked — inserted once, else "Not pasted: the field changed"; several
lines are not pasted; a refused read says so. A pending read keeps its session through iOS's
prompt and one reconnect, unless a batch is unanswered. A long
press on the bar shows a trace of input events, never a password's characters. The guard checks the destination when the keys start: a page that
moves the focus from its own input handler while they arrive (split one-time-code boxes) gets the
rest where it moved it, as a keyboard would. `make test-browser-viewer-engines` checks the viewer in
WebKit and Chromium with touch.

**The probe** (every 0.3 s and after each input, while a `/ctl` socket is open) reads, in each
frame's isolated world, the editable fields' rectangles, types and focus (date and time
fields included — the phone's keys reach their segments: digits, A/P, ← →, Backspace; a range
or colour is no field here) (the visible ones, and
the focused one wherever it is), a non-password field's
value (≤ 256 characters) and **a password field's length only**. The viewer gets no field's value
at all — rectangles, types, labels, focus, keys, whether it holds text and the
password-at-first-sight flag only. It
goes only to the current viewer: it is not an observation and
is never logged, stored or shown to a model. **Selection:** the X selection is cleared when the
view opens and after each read, and a new one of at most 200 characters is offered to the
viewer as a one-tap Copy in the viewer's bar beside the site's name (never over the picture,
Chrome's toolbar included, or a button). Looked at before and after the read, it is never
offered from a page holding a typed password — a password field with a value, focused or not
(a Tab into a filled one selects its value, which Chrome puts in the selection, and the page can
move the focus away again) — nor while the focus is in a frame whose focused field the probe
cannot see (`liveview.selection_offerable`), nor in the 2 s after the viewer's own select-all
(it selected the old address).

**Reconnect.** A phone that suspends the app leaves both sockets dead, sometimes still marked
open. The viewer, on `visibilitychange`, `pageshow` and `online`, closes both and reopens them,
showing "Reconnecting…"; it also reopens a socket that closed (1–8 s backoff) or whose `/ctl`
heartbeat (every 5 s) is 12 s old. Attempts never overlap: only the latest opens sockets, and a
socket the server replaced (4002) makes the page ask rather than give up. A refused reconnect asks
`view/{run}/state`: Done pressed elsewhere → the Done screen (the closing page); another viewer or an ended view →
it says so and stops. Reconnecting
claims nothing and resumes nothing: the run stays `live`, the model paused.

**Done.** The viewer closes both sockets and posts `view/{run}/done`: the phase becomes
`review`, the screen lock drains, and every window the view resized goes back to its bounds
before the view (Chrome as launched, 1919×1079 at 0,0 on the 1920×1080 screen — Chrome without a
window manager keeps a normal window one pixel short of the screen), verified by reading back the
bounds and each page's `screen`. The controller's Done (`live_done`, and `/v1 …/live-complete`,
which never passes through the page) runs the same restore first; an unverifiable restore
interrupts the run. **Done is one tap**: the controller's Done runs at once and the
page says so ("Done. Fido is continuing — you can go back to Telegram"); a sign-in typed in the
view is saved by the sign-in check, with nothing to confirm
([`casa-browser-credentials.md`](casa-browser-credentials.md)), and nothing is asked (operator
ruling 2026-10-31): a `login` view's Done learns a sign-in provider binding by itself
([`casa-browser-runs.md`](casa-browser-runs.md)). `view/{run}/review`, where a viewer page goes
once Done was pressed here or on another device, is the same closing page (`ui.py::Ui.review`).

## Known limits

- The 1Password extension inside the remote Chrome shows its own "Save login? — unlock
  1Password first" frame after a sign-in (its guest is read-only). No Chrome-managed setting
  of the extension turns it off; 1Password's account policies and the extension's own setting
  ("Offer to save logins", per profile) are outside the app.
- The pages never show an account page's address. The Sites page shows each site's registered addresses, and the run page each
  file's SHA-256, as one-tap copies.
- The Clipboard API needs a secure context: over plain `http` only the copy fallback works.
  Pasting is the strip's Paste.
- A key does in Chrome what Chrome does with it (a cluster, a length limit, a formatter, a caret
  the page moved), seen live. A CJK composition goes only as its text at its end.
- Text for Chrome's address bar is always `xdotool`'s (the page's input cannot reach the
  toolbar), so an accent or an emoji typed there can still be lost on a busy machine.
- What the real phone does (the keyboard and its geometry, whether it opens for the address-bar
  tap, iOS's own scrolling, an inert pinch, the strip keeping the keyboard up, Paste, AutoFill noise, the header in the
  Companion app, reconnect through Home Assistant's own proxy) is checked on a phone, not by
  these tests.

## Invariants

**INV-BRW-030**: Only Home Assistant's ingress peer reaches the UI — nginx admits that one address and the UI requires a well-formed `X-Ingress-Path` — and every mutation needs a single-use nonce issued for its own path from a same-origin page.

**INV-BRW-031**: No operator input reaches Chrome before the view's session exists, which is only after the run entered `live` and, on an account session, its manual lease and (login/assist) the broker's capture acknowledgement, nor after any end of the view, which takes effect before anything else is awaited and lets only the operation in flight finish; a page that is no longer the current viewer gets no socket and no input.

**INV-BRW-032**: A live view returns its run to `running` only after its sockets are closed and every window it resized is back to its earlier bounds, verified with the screen at 1920×1080; a restore that cannot be verified interrupts the run.

**INV-BRW-055**: What the probe reads goes only to the current viewer — never to a log, a model or any other surface; it holds a password field's length, never its value.

**INV-BRW-034**: A provider binding is shown as bound only when it was learned under the proxy's current binding key; one learned under another key (an HA restore makes a new one) or under no recorded key is shown as needing a live login, and forgetting the site clears it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/ui.py`
- `casa_browser/rootfs/opt/casa_browser/i18n.py`
- `casa_browser/rootfs/opt/casa_browser/liveview.py`
- `casa_browser/rootfs/etc/nginx/casa_browser.conf`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-nginx/run`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-vnc/run`

**Tests**
- `casa_browser/tests/test_ui.py`
- `casa_browser/tests/test_liveview.py`
- `casa_browser/tests/test_ingress.py`
- `casa_browser/tests/test_i18n.py`
- `casa_browser/tests/integration/test_ingress.py`
- `casa_browser/test/viewer_engines.py`

**Related**
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-approvals.md`](../reference/casa-browser-approvals.md)
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-contract.md`](../reference/casa-browser-contract.md)
<!-- END SOURCEMAP -->
