---
last_reviewed: 2026-10-21
---

# Casa Browser — the Chrome runtime and observations

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the optional `casa_browser/` app runs its browser: branded Google Chrome on a virtual
display, as its own service user, driven by Patchright from the controller, with one profile
per registered site and an ephemeral profile for public browsing; and what a model may see of
a page — masked text and opaque handles behind the page-view gate. The network the browser
sees — the kernel floor and the enforcing proxy — is
[`casa-browser-runtime.md`](casa-browser-runtime.md).

## Mental model

**The controller drives, the browser user runs.** Profiles belong to `casa-browser` (1100);
the run state belongs to `casa-controller` (1103). Patchright runs in the controller and
launches `bin/chrome-launch` as its browser executable; that wrapper hands its descriptors to
the browser user's launcher, which starts Chrome. A compromised Chrome therefore holds the
browser user only — not the controller's memory or store.

**One session at a time.** A session is one Chrome process tree on one profile. Switching from
one site to another, or to the public session, closes the previous Chrome completely first;
nothing but the run's own masked history crosses.

## The launcher

`casa_browser/rootfs/opt/casa_browser/launcher.py::Launcher` is the service
`svc-browser-launcher`, running as `casa-browser`. It listens on
`/run/casa-browser/launcher/launch.sock` and admits only a peer whose `SO_PEERCRED` uid is
`casa-controller`. A launch request carries, with `SCM_RIGHTS`, the wrapper's stdout, stderr
and the two CDP pipe ends, plus Patchright's argv and the profile name:

- no launch unless the network floor's root-owned marker exists
  (`netfloor.py::floor_installed`);
- the profile is `site:<id>` (`/data/profiles/<id>`, `0700`, a symlink refused) or
  `ephemeral` (a fresh directory removed when Chrome exits); the argv's own `--user-data-dir`
  is replaced;
- the argv is an allowlist (`launcher.py::chrome_argv`): exactly the switches Patchright and
  the launch contract emit, with fixed values for the proxy, the window, the colour profile,
  the password store and the blink features, feature lists limited to their known items, and
  `--remote-debugging-pipe`, the enforcing proxy and the removal of the loopback bypass each
  required exactly once (`launcher.py::REQUIRED_ARGS`); anything else — a sandbox, debugging, extension or
  command-prefix switch — and any page argument except `about:blank` is refused
  (`--hide-crash-restore-bubble` is on the list and in every launch: a tab Chrome restores while
  Patchright is attached never finishes loading, so its "Restore pages?" bubble is not shown);
- Chrome's environment is built by the launcher (`launcher.py::chrome_env`): display, X cookie,
  a validated time zone, and a home directory whose NSS database trusts the proxy's CA;
- one Chrome at a time; Chrome runs in its own session, and when the wrapper's connection
  closes — Patchright killed it, or the controller died — the launcher kills that whole
  session; when Chrome exits, its status goes back to the wrapper. The wrapper arms a death
  signal from its parent and gives up if the parent was already gone
  (`launcher.py::arm_parent_death`), so it never holds a Chrome for a dead driver. The passed
  descriptors are received close-on-exec, so no other connection's descriptor can reach a
  Chrome started meanwhile; and a launcher that starts — after its own crash or restart —
  first signals any Chrome of its user that the previous one left (`launcher.py::reap_leftover_chrome`),
  and it reports stopped or admits a new launch only under the exit proof below.

`BrowserManager.close(session=..., force=True)` checks the exact current session and sends
an authenticated `close` request to the launcher. `Launcher.close` kills Chrome's process
group and waits up to five seconds for supervision to confirm exit, including all executing
process-group members. Exited zombies cannot execute page work and do not delay confirmation.
A missing or failed acknowledgement leaves the manager's session in place and propagates a
failure to its caller. This forced path kills before asking Patchright to close its context.
The exit proof is one stateless rule (`launcher.py::chrome_alive`): no process of the
browser user whose command line is a program in Chrome's directory is alive, zombies
excepted. It needs no record of which processes were started or inherited, so it covers a
session's whole process group, a Chrome left by an earlier launcher, a child forked after
any kill, and a process left in uninterruptible sleep; a pid reused by another program is
never Chrome, so reuse cannot hold the launcher. Unreadable state of a process of that user
cannot confirm exit. Each check kills again every Chrome it finds, and confirms exit only
after two consecutive empty scans `EXIT_SCAN_GAP` (50 ms) apart, so a process forked while
the first scan ran — its parent exiting before the scan reached it — is seen by the second.
A chain of such forks, each timed inside one scan, is the stated residual. The proof gates the
launcher's stopped state, release of its single-Chrome lock, replacement of its process id
and removal of an ephemeral profile; an unconfirmed exit keeps those resources held, and a
background check repeats every 100 ms until exit is confirmed. Each close request still has
its five-second bound. Only the real process table is ever signalled.

`Launcher.forget` deletes one site's profile, never while a Chrome runs.

**INV-BRW-013**: Chrome starts only as the browser user, only from the launcher, only for a controller-user caller, only with the network floor installed, and always with the Chromium sandbox and a pipe as its only debugging channel.

The image disarms Chrome's setuid sandbox helper: Chrome uses its namespace sandbox, and
without user namespaces it refuses to start instead of falling back to a setuid binary.

## The browser session

`casa_browser/rootfs/opt/casa_browser/browser.py::BrowserManager` opens sessions.
`browser.py::launch_options` is the launch contract: branded Chrome headed under Xvfb,
`chromium_sandbox=True` (Patchright otherwise adds `--no-sandbox`), `no_viewport`, service
workers allowed, `--disable-background-networking` dropped from Patchright's defaults (without
it the policy-installed extension never downloads), every request through
`http://127.0.0.1:3128` with the implicit loopback bypass removed, the effective locale's
language as `--lang` and its time zone in the environment, a 1920×1080 window, and the ANGLE
Vulkan flags for the Intel GPU (one `--enable-features`, which also repeats Patchright's own,
since Chrome keeps only the last). Downloads are accepted into `/data/downloads`
(`downloads_path`; see [`casa-browser-files.md`](casa-browser-files.md)). No user agent,
header or viewport override is set.

`browser.py::BrowserSession` holds the Patchright context and a generation that grows with
every session; it is never serialised. `BrowserManager.next_generation` is the generation the
next `open` gives, so the run controller can install a session's policy before its browser
starts ([`casa-browser-runs.md`](casa-browser-runs.md)).

## Managed policy

`casa_browser/rootfs/etc/opt/chrome/policies/managed/casa.json` force-installs the official
1Password extension from the Chrome Web Store and pins its icon to the toolbar
(`toolbar_pin: force_pinned`, so the operator finds it in a live view), disables browser sign-in, sync, the password
manager and its leak check, metrics reporting, DNS-over-HTTPS and the built-in DNS client,
QUIC, non-proxied WebRTC UDP, and address and card autofill (`AutofillAddressEnabled` and
`AutofillCreditCardEnabled` false — the first also turns off single-field form history, whose
dropdown covered the next field in the live view; it is marked deprecated in Chrome 156 for
`AutofillSettings`, which alone does not, and an image test pins it), and hides Chrome's warning bar about an unsupported
command-line flag (`CommandLineFlagSecurityWarningsEnabled: false`: Patchright's
`--disable-blink-features=AutomationControlled`, which keeps `navigator.webdriver` false, is on
Chrome's list), and allows the debugger on the extension's own pages
(`DeveloperToolsAvailability: 1`: Chrome 156 refuses it by default on a force-installed
extension's pages, and the 1Password guest's state is read from its popup). The extension's
download passes the proxy through the
Chrome update rules described in [`casa-browser-runtime.md`](casa-browser-runtime.md).

## Display

`svc-browser-xvfb` runs Xvfb as `casa-browser` on display `:99`, 1920×1080×24 at 96 dpi, with
no TCP listener and an X cookie (`/run/casa-browser/x/Xauthority`) only the browser user can
read. The display's two other readers, both for the live view
([`casa-browser-ingress.md`](casa-browser-ingress.md)), get their own copy of the same cookie,
each `0600` to its one reader: x11vnc (`/run/casa-browser/x-vnc/`, the picture) and the controller
(`/run/casa-browser/x-ctl/`, the operator's input and the X selection).

## Pacing

`casa_browser/rootfs/opt/casa_browser/pacing.py::Pacer` performs each action the way a person
would: scroll the target into view, refuse a target that is not visible and enabled, move the
pointer along a curve to a point inside it, type with a bounded random delay per character,
scroll in steps. Every operation on the page — reaching the target, each key, each wheel
step — returns control within `pacing.py::ACTION_TIMEOUT` (5 s) with a typed
`pacing.py::ActionError` rather than retrying; typing a long text at that pace may take longer
in all and is never cut short. For the action gate a judged press takes a `guard`, awaited
after the pointer has moved and immediately before `mouse.down()` (only `ok` presses);
`type` checks that the target holds the focus before every character, sends each as one
printable `keyboard.type` and never Enter, which only `Pacer.press_enter` sends behind its
own guard, and inserts a line break as text (`keyboard.insert_text`, no key event) only when
its caller allows line breaks, refusing the text before any click otherwise; `scroll` checks before every wheel step that the point is over no form control,
editable element or frame and moves off one, or refuses (`unsupported`)
([`casa-browser-actions.md`](casa-browser-actions.md)).

## Observations and handles

`casa_browser/rootfs/opt/casa_browser/observe.py::Observer` produces the only view of a page a
model gets, and the operator's screenshots.

**The page-view gate.** `observe.py::page_view_allowed` asks whether the page belongs to the
session: the run policy, its leases removed, decides the page's full current address —
fragment and client-side route included — as a top-level document. In an account session that
is an address on the site's origins or login origins (`site_document`); in the public session,
any address the floor admits (`public_session`). A lease, an infrastructure host (the
1Password web app, a captcha endpoint) and any non-web scheme (`chrome://`,
`chrome-extension://`, `about:`) never qualify. The gate runs on the address Patchright reports and again on the
address the extraction itself read; nothing is read while a live view or a broker login holds
the browser.

**Extraction.** Fixed code (`observe.py::EXTRACT`), evaluated once per frame in the isolated
world, reads the document and every open shadow root. Each child frame passes the one frame
rule (`observe.py::frame_shown`, which the action check's page reading uses too), parents
before children (`observe.py::child_frames`): every child frame needs a shown parent and a
document origin, as its own extraction reports it, that is not opaque; then a frame with a web
address is shown when the gate admits it, before the extraction and as the extraction itself
read it, and a frame without one (`about:blank`, `about:srcdoc`, a document the page wrote with
`document.open`, as a code playground's result frame is) has no address of its own and is shown
when its origin is its parent's — and an address its extraction reports is admitted too. Any other frame (in an account session, a
frame off the site, a frame sandboxed to an opaque origin, any frame inside a withheld one) is
left out and its place noted; a frame sandboxed with `allow-same-origin` keeps its own origin
and is judged like any other. Inside a shown frame the fields follow the same secret rule as the page's own. Afterwards the page generation
(`browser.py::BrowserSession`, advanced on every navigation of any frame) and the address are
checked again, and the result is discarded if the page moved. A browser-side failure becomes a
typed code, never the browser's error text. `Observer.observe_page` returns, beside the
observation (whose `source_id` is always empty here), the address and document title the
extraction read — controller-private, for the run controller's source record.

**What the model receives.** Text is the visible text of each block, one block per line:
visibility is the browser's own judgement (`checkVisibility`, so a hidden ancestor,
`visibility`, opacity and a closed `<details>` count; text under a `display:contents` element is
judged on the nearest element that renders a box), and a block is the nearest ancestor that is
not inline-level, so a value split across inline elements stays whole. Form fields and editable
content are never read — editability is the browser's own too (`isContentEditable` or a computed
`-webkit-user-modify` that writes), followed up through shadow hosts. The text passes
`casa_browser/rootfs/opt/casa_browser/masking.py::mask_text`: every address, wherever it starts
and up to the next whitespace — `scheme://…`, and for http, https, ws, wss, ftp and file every
form the URL standard accepts (any run of slashes or backslashes after the colon, or none) —
becomes its origin (only after two or more separators and never for `file:`; otherwise, or when
its host is not plainly a host name or address, `link`) and an opaque reference
(`https://site.example ⟨r1⟩`) — a credential inside an address goes with the whole address —
and, outside addresses, strings in known credential formats and long high-entropy tokens become
`[masked]`. Text is masked whole and cut to its limit afterwards, and every cut made before
masking drops the word it would split, and a block that reaches its limit takes no further text,
so no limit leaves part of a value that masking would have recognised. Elements become
`ElementView`s: a random handle, a role from a fixed set (any other `role` attribute falls back
to the element's own), and a masked label taken only from visible, non-editable text — the
elements `aria-labelledby` names, the field's label, or the element's own text — never from an
attribute and never from a field's value; an unnamed control is `(unnamed button 2)`. A field of
`gate.py::FIELD_KINDS` also carries its `kind` (`time`, `date`, … `number`, and `multi-line`
for a real `<textarea>`, the one field that takes line breaks), a closed-set fact like the
role, never the attribute's text. An ordinary field also carries what it holds (operator
ruling 2026-10-31: the agent picked a date in a calendar widget and could not confirm it): `value_masked` — an input's or textarea's value, a select's chosen
options (each label as the page shows it), an editable element's text, read up to 500 characters (a longer one is cut as text is,
ending in ` …`) and masked like page text — or `checked` for a checkbox, a radio or an
`aria-checked` control. The rule is the screenshots' one: a field the gate's secret rule
recognises (`gate.py::SECRET_JS`; a password, one-time-code or payment-card field, or one inside
or holding one) carries neither, judged as the field is when it is read (operator ruling
2026-10-31, option (b)). Right after the reading every field whose value would be shown is asked
the rule again (`Observer.observe_page`), as a screenshot is re-checked while its marks are on;
one that answers secret now, or cannot be asked, shows nothing. This
loosens the original design (an observation carried no field values) deliberately; the action
check's packet already showed ordinary values under the same rule, and nothing the gate decides
reads an observation. Residual (ruling 2026-10-20, a page can show its own value as text): a
field the page itself later changes — its type or autocomplete removed, as a "show password"
toggle may do, or moved out of a secret element — shows what it then holds, and a value typed
into a field the rule does not recognise is an ordinary value. Which elements are listed is one rule shared with the
action gate's reading (`gate.py::CONTROLS_JS`): the semantic controls first, up to the
cap of 400 (reaching it makes a reading incomplete); then, only in the room the cap leaves, what
the page makes actionable without saying what it is — an element in the tab order or with an
inline `onclick`, and where the page shows a pointer cursor each element holding its own text
(a list entry, a calendar cell) and each icon — as role `clickable`, never a label or anything
inside or holding a semantic control. A widget whose parts carry none of these facts is not
listed (the live view is its way). A visible
select is followed by its options (role `option`, named by their text), each a `select`
target even while the dropdown is closed. The real
target stays in `casa_browser/rootfs/opt/casa_browser/handles.py::HandleRegistry`, bound to its
run and page generation; `handles.py::StaleHandle` refuses it from another run or an older
generation. Two residuals, both properties of the string: a short code such as an 8-character
recovery code is not caught, and a value the page itself splits — across separate layout boxes
(block children, flex or grid items, a slot) or by whitespace inside an address (a tab or line
break the URL standard would skip) — is judged piece by piece, so its parts can reach the model
unmasked. Only pages that belong to the session are observed at all.

**Screenshots** — the operator's own (`Observer.capture_operator`: after an action, and the
picture the agent sends with `take_screenshot`) and a question's (`Observer.capture_question`, which also outlines the
target, [`casa-browser-actions.md`](casa-browser-actions.md)) — are for the operator only, never
for a model, and are taken under one rule (`Observer._capture`).
Both are cropped to the page's content (`crop_png`), so a small page is legible on a phone: the
union of the viewport rectangles of its visible text, images, media, frames, form controls and
shadow hosts, in the document and every open shadow root, and a question's target, with a 24 px
margin and at least 480×240 pixels. It is measured right after the capture, with the page's
animations held as the capture held them (finite ones finished, looping ones at their start), and the captured image is cut to it once the capture's checks passed; the
whole image is kept when that fails. A crop only leaves out window area; the mask and its checks
are the same.
Every form field and editable element — in the document and every open shadow root,
inventoried without the observation's limits — that the gate's one secret rule recognises (a
password field, a one-time-code or payment-card field, by `type` or `autocomplete`, or one inside
or holding such a field; `gate.py::SECRET_JS`), and every child frame, is blacked out by
Playwright's screenshot mask. Every other field shows what it holds — a name, an email, a choice,
a comment — and text the page itself renders stays visible, secret-looking or not (operator
rulings 2026-10-20 and 2026-10-29: simple redaction of recognised secret fields; the screenshots
are the operator's own). No screenshot is taken when the
inventory hit a limit, when editable text renders outside the box of an editable element (an
editable element with no box of its own, or overflow left visible), so the mask could not cover
it, when the page holds a closed shadow root (asked of the browser through CDP `DOM.getDocument`
with `pierce`; a failure to ask counts as one; a field inside it could not be masked), when the
page does not belong to the session or moved, or when a second reading taken right after the capture, while the
marks are still on, finds any of the conditions above (a limit reached, a closed shadow root),
an element the mask was to cover gone or unmarked, or any secret field or frame without the capture's
mark (a secret field or frame added, replaced or swapped during the capture). Only the final PNG is written, under
the artifacts directory.

**INV-BRW-043**: An observation or a screenshot is produced only when the page-view gate admits the page's full current address as the session's own — in an account session, the site's origins or login origins; in the public session, an address the floor admits — before and after it is read, and an observation carries no address beyond an origin, no attribute, no selector and no value of a field the one secret rule recognises — a password, one-time-code or payment-card field, as the field is when it is read — each field whose value it shows being asked that rule again right after the reading.

## Testing

`casa_browser/tests/test_browser_config.py` pins the launch contract, the managed policy, the
launcher's checks and the pacing primitives. It also pins
exit confirmation, retention of the lock and profile until exit, and retention of the
driver's credential guards across an unconfirmed inherited process. In the image lane,
`casa_browser/tests/integration/test_browser_runtime.py` runs the real Chrome as the browser
user from a controller-user process: the namespace sandbox and command line, the effective
policies, profile persistence and isolation, `forget`, the floor gate, and the extension's
installation into two new profiles through the production proxy (this needs the Chrome Web
Store). `casa_browser/tests/integration/test_proxy_enforcement.py` drives the real browser
against synthetic fixture sites behind the test harness proxy and counts upstream receipts.
`casa_browser/tests/test_{masking,handles,observe}.py` pin masking, handles and the gate with
fakes; `casa_browser/tests/integration/test_observation.py` runs the real extraction code on
branded Chrome, including shadow roots, split and long secrets, editable content and fields
added or replaced during the capture, and checks the screenshot's masked regions pixel by pixel.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/launcher.py`
- `casa_browser/rootfs/opt/casa_browser/browser.py`
- `casa_browser/rootfs/opt/casa_browser/pacing.py`
- `casa_browser/rootfs/opt/casa_browser/handles.py`
- `casa_browser/rootfs/opt/casa_browser/masking.py`
- `casa_browser/rootfs/opt/casa_browser/observe.py`
- `casa_browser/rootfs/opt/casa_browser/bin/chrome-launch`
- `casa_browser/rootfs/etc/opt/chrome/policies/managed/casa.json`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-launcher/run`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-xvfb/run`

**Tests**
- `casa_browser/tests/test_browser_config.py`
- `casa_browser/tests/test_masking.py`
- `casa_browser/tests/test_handles.py`
- `casa_browser/tests/test_observe.py`
- `casa_browser/tests/integration/test_browser_runtime.py`
- `casa_browser/tests/integration/test_proxy_enforcement.py`
- `casa_browser/tests/integration/test_observation.py`

**Related**
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
<!-- END SOURCEMAP -->
