---
last_reviewed: 2026-10-21
---

# Casa Browser — the Chrome runtime

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the optional `casa_browser/` app runs its browser: branded Google Chrome on a virtual
display, as its own service user, driven by Patchright from the controller, with one profile
per registered site and an ephemeral profile for public browsing. What a model and the operator
see of a page is [`casa-browser-observations.md`](casa-browser-observations.md). The network the
browser sees — the kernel floor and the enforcing proxy — is
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

**A switch keeps a site's sign-in within the run.** Session cookies die with Chrome, so before an
account session closes for a switch `controller.py::RunController._keep_session_cookies` reads those
with `expires == -1` into memory on the run, by site id; `RunController._restore_session_cookies`
puts them back with `context.add_cookies` when that site's session reopens in the same run, before
it loads anything. Only a session of that site id gets them. They are never logged, sent or stored,
and go when the run's ending begins, which a forget and a re-pair both start. A failure on either
side is logged without values and the run goes on signed out. A forced close never reaches the
read, so there the sign-in is lost; `sessionStorage` is not kept.

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
would: scroll the target into view, refuse one it cannot press and say why
(`pacing.py::TARGET_REFUSALS`: `not_visible`, checked before the scroll as well because a hidden
target's scroll waits out its timeout, after a moment for a page in a transition
(`pacing.py::Pacer.shown`), `disabled` or `no_size`, each reaching the step's reply as its
`not_run` reason), move the pointer along a curve to a point inside it, type with a bounded random delay per character,
scroll in steps. Every operation on the page — reaching the target, each key, each wheel
step — returns control within `pacing.py::ACTION_TIMEOUT` (5 s) with a typed
`pacing.py::ActionError` rather than retrying; typing a long text at that pace may take longer
in all and is never cut short. For the action gate a judged press takes a `guard`, awaited
after the pointer has moved and immediately before `mouse.down()` (only `ok` presses);
`type` selects what an `<input>` or a `<textarea>` holds so the text replaces it (an empty text
clears it; a contenteditable takes the text at the caret), checks that the target holds the focus
before every character, sends each as one
printable `keyboard.type` and never Enter, which only `Pacer.press_enter` sends behind its
own guard, and inserts a line break as text (`keyboard.insert_text`, no key event) only when
its caller allows line breaks, refusing the text before any click otherwise; `Pacer.insert`
puts more than 200 characters into a real `<textarea>` (no Enter after it) as one insertion
replacing what it holds, behind the same guard and focus check; `scroll` checks before every wheel step that the point is over no form control,
editable element or frame and moves off one, or refuses (`unsupported`)
([`casa-browser-actions.md`](casa-browser-actions.md)), and answers where that left what it scrolled
(`pacing.py::_position`: the outermost scroller that moved, or with none the innermost at the
pointer, so a page that scrolls inside a box reports the box). The controller's
`_scroll` uses `Pacer.scroll_to` the first time a step scrolls by a handle: it brings the element into
view, and when that moved anything that is the whole step. The same handle again wheels on from where
the page is, never back to the element, so repeated scrolls advance; a new observation issues new
handles, and any other action starts over.

**INV-BRW-068**: A repeated `scroll` on one handle advances instead of going back to its element, the first brings that element into view, and every scroll's reply states how far down of how far in all, and whether it moved.

## How a `type` reaches its field

What the action gate does around these ([`casa-browser-actions.md`](casa-browser-actions.md)) decides
whether a `type` runs at all; this is how it then reaches the page.

**Value fields.** A `type` into a `time`, `date`, `datetime-local`, `month`, `week`,
`range` or `color` input (`gate.py::VALUE_TYPES`) is judged with the same packet as typing, then
set as one in-page step (`gate.py::_SET_VALUE`, the select's pattern: F4 and a tap's final read
first; no pointer, no keys): the field must still have the frozen tag and type and be visible,
enabled and writable; a text the browser's own value format refuses puts the old value back and
fires nothing (not dispatched); otherwise `input` and `change` fire and the value is read again
after the page's listeners ran — `typed: shown`, or `not_shown` when the page changed it (it
ran). What the page does in response is part of the action, as for typing and selecting.
Before any packet, `_admissible` answers `not_run` with `not_typable` (a read-only field),
`value_format` (the text a detached input of the same type, the field's `min`/`max`/`step`
copied, does not take) or `no_enter` (Enter after a value field). Keyboard entry into these
fields depends on the display locale and on the segment a click lands on, so it is never used.

**Keyboard.** `type` targets an editable text control only, never a password, one-time-code or
payment-card field (refused before any packet); in an `<input>` or a `<textarea>` the text
replaces what the field holds (`Pacer.type` selects it after the click; an empty text clears it, one
empty insertion behind the same focus check), and a contenteditable takes it at the caret, so a
signature or a quoted reply stays; its
text holds printable characters only (`protocol.py::printable_text`); every character is sent
only while the target holds the focus and is still an editable text control; the only non-printable key is Enter (`enter: true`),
sent by `Pacer.press_enter` behind its own guard. **Line breaks** are the one
exception to printable-only: the parser takes LF (CR and CRLF arrive as LF), and the text is
judged whole, its line breaks in the packet's `action.text`. They go only into a real
`<textarea>` (`_TEXT_FACTS`' `textarea`; the observation shows it as kind `multi-line`) and are
inserted as text (`keyboard.insert_text`: no key event, so a page's Enter handler never sees
one), each behind the same focus check, which also requires the field to be that textarea
still. Before any packet `_admissible` answers `not_run` with `one_line` for a line break into
any other field (an `<input>`, which takes an inserted line break as implicit submission, a
contenteditable editor, a value field) and with `no_enter` for Enter after one (sending stays its
own checked step), and the pacer
refuses a line break its caller did not allow before anything is clicked. For a `type` that ends in Enter the dispatch
is the Enter: after a go, the field's facts are read again with the value read back after the
last keystroke; after a tap, the typed page is re-judged first. A `type` without Enter
reads the field back after the last key and says only whether it shows the text (`typed`), so a
field that refused the keys is not reported as filled; an empty text is shown only by a field that
is empty.

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

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/launcher.py`
- `casa_browser/rootfs/opt/casa_browser/browser.py`
- `casa_browser/rootfs/opt/casa_browser/pacing.py`
- `casa_browser/rootfs/opt/casa_browser/bin/chrome-launch`
- `casa_browser/rootfs/etc/opt/chrome/policies/managed/casa.json`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-launcher/run`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-xvfb/run`

**Tests**
- `casa_browser/tests/test_browser_config.py`
- `casa_browser/tests/integration/test_browser_runtime.py`
- `casa_browser/tests/integration/test_proxy_enforcement.py`
- `casa_browser/tests/test_pacing.py`
- `casa_browser/tests/integration/test_long_page_find.py`

**Related**
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-observations.md`](../reference/casa-browser-observations.md)
<!-- END SOURCEMAP -->
