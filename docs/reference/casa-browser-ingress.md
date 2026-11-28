---
last_reviewed: 2026-11-28
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
([`casa-browser-runs.md`](casa-browser-runs.md)). The live view itself — its grants, sockets, window,
input and probe — is [`casa-browser-live-view.md`](casa-browser-live-view.md).

## Mental model

**Three processes, one door.** nginx (user `casa-ingress`, uid 1105) listens on port 8099 and
admits only Supervisor's ingress address — `allow <peer>; deny all;`, rendered at start by
`python -m casa_browser.ui ingress-peer` (a missing file stops nginx from starting). It serves
the app's own `static/` files and noVNC's `core/` and `vendor/` modules (Debian's 1.6.0 files,
nothing of noVNC's own app), and proxies everything else, WebSocket upgrades included (but the
Saved logins page's two locations in built-in mode, below), to the UI's Unix socket `/run/casa-browser/ui/ui.sock` (directory `0750` controller:casa-ingress). The
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

With the app's own login store (`Ui.builtin`) the Sites texts speak of a saved login instead
of a 1Password item (`Ui._login_key` picks each text's `.builtin` key, which names neither
1Password nor its extension), and `/setup` shows one row saying where logins are kept, or that
they could not be opened when the broker does not report its store available
(`Ui._builtin_store_row`), the language row, a line on what saved logins are used for, and
pairing: no guest state, counts or warning. In that mode the store's row, while the store is
open, and the page of a site with sign-in addresses (a **Saved logins** row) link to
`<prefix>/logins/`, the Saved logins page the broker serves; `/sites/new` has no box for a
login's id, and `/sites` ignores one posted. The Sites list and a site's page first refresh the
controller's copy of the links (`RunController.refresh_links`). The page, nginx's
`logins.conf` (included in the ingress server after the peer rule) and the `logins-conf`
command that writes it are
[`casa-browser-builtin-store.md`](casa-browser-builtin-store.md).

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

The live view a run hands to the operator — its grants, sockets, window, input, probe,
reconnect and Done — is described in [`casa-browser-live-view.md`](casa-browser-live-view.md).

## Known limits

- The pages never show an account page's address. The Sites page shows each site's registered addresses, and the run page each
  file's SHA-256, as one-tap copies.
- The Clipboard API needs a secure context: over plain `http` only the copy fallback works.
  Pasting is the strip's Paste.

## Invariants

**INV-BRW-030**: Only Home Assistant's ingress peer reaches the UI — nginx admits that one address and the UI requires a well-formed `X-Ingress-Path` — and every mutation needs a single-use nonce issued for its own path from a same-origin page.

**INV-BRW-034**: A provider binding is shown as bound only when it was learned under the proxy's current binding key; one learned under another key (an HA restore makes a new one) or under no recorded key is shown as needing a live login, and forgetting the site clears it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/ui.py`
- `casa_browser/rootfs/opt/casa_browser/i18n.py`
- `casa_browser/rootfs/etc/nginx/casa_browser.conf`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-nginx/run`

**Tests**
- `casa_browser/tests/test_ui.py`
- `casa_browser/tests/test_ingress.py`
- `casa_browser/tests/test_i18n.py`
- `casa_browser/tests/integration/test_ingress.py`

**Related**
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-approvals.md`](../reference/casa-browser-approvals.md)
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-contract.md`](../reference/casa-browser-contract.md)
- [`reference/casa-browser-live-view.md`](../reference/casa-browser-live-view.md)
<!-- END SOURCEMAP -->
