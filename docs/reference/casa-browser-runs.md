---
last_reviewed: 2026-10-22
---

# Casa Browser — the run controller

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the optional `casa_browser/` app runs a browsing task: one leased run at a time, its
browser sessions and policy, its broker and driver calls, its live-view states and every way
it ends — `casa_browser/rootfs/opt/casa_browser/controller.py` (`RunController`, which the
`/v1` routes of [`casa-browser.md`](casa-browser.md) await) and the s6 unit
`svc-browser-controller`. The components it composes have their own documents: the request
policy and the store ([`casa-browser.md`](casa-browser.md)), the floor and the proxy
([`casa-browser-runtime.md`](casa-browser-runtime.md)), Chrome, observations and handles
([`casa-browser-browser.md`](casa-browser-browser.md)), downloads
([`casa-browser-files.md`](casa-browser-files.md)), the operator's questions
([`casa-browser-approvals.md`](casa-browser-approvals.md)), and the broker and login driver
([`casa-browser-credentials.md`](casa-browser-credentials.md)), and the ingress pages and the
live view's screens, sockets and window ([`casa-browser-ingress.md`](casa-browser-ingress.md)),
whose `LiveView` is the controller's grant issuer.

## Mental model

**One run holds the browser.** Runs queue in FIFO order (`Store.lease_next_run`); the leased
run is the controller's `active` run, and every other run waits. The lease is released only
when the run's browser is confirmed stopped and its policy withdrawn.

**One command lock.** Everything that touches the browser, the proxy, the broker or a run's
state runs under it. A run-scoped command first checks the run's state in the store and
refuses at once when the run cannot act (`not_ready` while queued, starting, waiting, live or
finishing; `interrupted`; `conflict` for another end), so a model call never waits behind a
run's start or a two-minute login; it checks again once it holds the lock. The command's work
is a child task: `cancel`, an interruption and `request_live` act on it before they wait for
the lock — cancel and interruption cut it at once (the cut command answers `interrupted`), a
live view lets it finish for up to 15 s and then cuts it (`not_ready`).

**The proxy is told, and acknowledges.** Every policy install raises the run's revision,
names the proxy start it expects (a restarted proxy refuses it), and returns only once the
proxy holds the policy. One event consumer reads the proxy's events for the active run every
0.5 s — and once more at a live view's Done, after its lease ended — and hands each event of a
login view to its provider-binding learning; no other event is kept.

## Starting a run

`start_run` validates before queueing: the effective locale is known, an account run has
1Password configured (an open-web run does not need it), and every granted site exists; the
same `request_id` with the same members returns the existing run and starts nothing. Leasing
re-reads the granted sites (a site gone, or the setup no longer ready, ends the run `failed`),
then: the broker's `begin_run` (account runs); the first session — the first account grant's
profile, else the public session; `status()` for the proxy start; before a browser starts while
no Chrome runs, the credential window a started proxy opened is closed with the token
`status()` reports (never while Chrome runs); the session's policy; the browser; then the site's
start page, with the login sequence below (a start page the policy refuses is not loaded and is
logged as `page_unreachable`). Opening a session runs no 1Password guest unlock: that waits
until a sign-in meets a passkey hint or a sign-in live view is requested (**The guest unlock**,
below). An open-web
spec's `start_url` is opened the same way in the public session, as a free public navigation
(the floor, G1's public-session check before the load); a refusal is `page_unreachable` and the
run starts on a blank page.

**Login.** Whenever the controller navigates an account site's start page: a `login` driver
session (which begins the first login lease), the start page, the broker's `login`, then
`close_session`. The `login` call carries `guest_ask`, true while the guest flow has not been
started for this browser session. `logged_in` continues, and so does `no_form` (the sign-in filled nothing and
found no form, or answered a passkey prompt: it claims nothing, and the model reading the page
decides whether he is signed in or needs the live view); any other status appends one `login_needed` event
whose outcome is the broker's fixed reason code (`password_change_page`, `rejected`, …) and the run
waits for the model's `request_live(…, 'login')`. A broker RPC error interrupts the run. A
`not_found` (no linked item) first lists the vault and starts a `login_item` judgement (once per
site per run; the login question, [`casa-browser-actions.md`](casa-browser-actions.md)); only
when there is nothing to judge is it `login_needed`.

**Signing in off the start page.** `observe(run, sign_in=True)` first looks at a page of the
account site that shows sign-in fields (`RunController._look`: mechanical facts, then the
broker's `read_sign_in` in a `login` driver session) and returns its `sign_in` judgement; its
verdict signs in on that page as it is (`RunController._auto_login`, the broker's `login` with
`auto`, no `_goto`). Every executed step clears the run's `looked` pairs, every sign-in adds the
page it ended on, and every sign-in that acted counts in the run's per-site ledger
(`RunController._ledger`) — [`casa-browser-signin.md`](casa-browser-signin.md).

**The guest unlock** (the browser's 1Password extension, signed in as the guest on a fresh
profile, unlocked after a browser restart; slice 5v) runs at most once per browser session, only
for an account profile with login origins, and only when needed: the active run's `guest_for` holds the
session it was last started for, set before the flow is awaited, so a flow that was cancelled,
failed or timed out still counts for that session (every browser start relocks the extension, so
another session object has not had it). The flow is a `guest` driver session, the broker's
`unlock_guest`, `close_session`; best effort: a state other than `unlocked` or `signed_in` is
logged and asks for nothing; a broker RPC error interrupts the run. Two things start it:
- A sign-in's `needs_guest` (the broker read a passkey hint with `guest_ask` set,
  [`casa-browser-credentials.md`](casa-browser-credentials.md)): the guest flow runs, then the
  sign-in once more from the start page with `guest_ask` false, so a sign-in retries at most
  once. When the driver closed the browser under the flow (K1) there is no retry and the run
  reports `login_needed` (`timeout`). A sign-in with no hint (a password, an emailed code, a
  start page already signed in by cookie) opens no guest session at all.
- `request_live(…, 'login')` on a site with login origins (below).

A run also gains a site from the operator's Yes on a sign-in card: the site's account grant
replaces any grant for it, the stored spec follows (the admitted digest stays), the broker's run
set is replaced, and the start page opens on the site's own profile, which signs in as above
([`casa-browser-signin.md`](casa-browser-signin.md)).
A `needs_code` reply (the site sent a code) is `login_needed` with outcome `needs_code`
("{site} asks for a sign-in code it sent."), and the controller keeps, in memory only, which
site, browser session and page generation asked; the active-run listing names that site as
`code_site`; any later login of the run clears it. `POST /v1/runs/{run}/login-code` (`login_code`)
takes the code for exactly that site, once: no report for it is `not_ready`; a browser session or
page generation that changed since, or another tab now the session's page, fills nothing
(`login_needed`, `code_page_gone`; the broker also requires its mark in that document); otherwise a
fresh `login` driver session, the broker's `fill_code` with that generation, then
`close_session` on every path (success, refusal, cancel, the run's end). It records a
`login_code` event that names the site but never the code, answers `done` when signed in, and
`login_needed` with the reason otherwise (`needs_code` again keeps a new report). A broker RPC
error interrupts the run, as for `login`.
`close_session` is the controller's only cleanup call and follows every path. When the driver
had to close the browser (an operation that did not drain, Task 9's K1), the controller drops
that session: the run's next step opens a new one (new generation, no live handles, its guest
not yet unlocked) and the cut login is reported as `login_needed` (`timeout`). A close the driver
could not confirm interrupts the run; that session is retired only after the launcher's exit
proof.

## Pages and actions

Every observation of an account run ends with one link per granted site, "Open site: {name}",
whose private target is the site's start page; a blank session shows only these. A navigation
— `navigate` on a link handle, or a `click` on a link to another session — goes to the session
the page belongs in (`controller._session_for`): an account grant's own profile; the public
session for a public grant, for every address off the run's sites (public browsing from a
signed-in task) and for every open-web page. Switching closes the former browser and gets the
launcher's exit proof before the new session's policy is installed and its browser starts, so
two sessions never run at once (no proof: the run is interrupted); the public session is the
ephemeral profile, which holds no account cookie and gets no 1Password unlock. Before it is
sent, a top-level navigation is asked of the run's policy with its leases removed; a refusal
(the floor) is `blocked`, never held. `click`, `type`, `select`, `scroll` and `back` act through `pacing.Pacer` on the
handle's element, bound to the page generation it was issued for (`stale_handle` otherwise); the
model never types into a password field (`unsupported` before any key). A download the browser
starts outside `POST …/download` is cancelled and deleted. After an action the operator-only
masked screenshot is attached to the `action` event. Every action, download and search first
passes the action gate ([`casa-browser-actions.md`](casa-browser-actions.md)): a checked one
answers `judgement_required` and runs only through its verdict; a question moves the run to
`waiting_approval` until its answer, a lapse or the run's end.

An observation replaces the run's earlier handles. When the page-view gate refuses the current
page (an account session's page that left the site), the observation is `blocked`; a closed
browser is a dependency failure and interrupts the run.

**Sources (v9.2).** An observation in the public session whose committed top-level address is
`https` and at most 800 bytes gets a `source_id` — the same one for the same address in the run,
at most 200 a run — and the address is stored with the masked title (≤ 120 characters) in the
store's `sources` table, swept with the run. Account-session pages never get one. The address
leaves the app only through `GET /v1/control/runs/{run}/sources`; `finish` naming an id the run
did not issue is `unsupported`.

**Search.** `search(run, query)` opens `https://www.google.com/search?q=<query>` in the public
session, whatever the run's sites (an account run moves to it), free of the action gate, and
returns its observation. In an open-web run a query that is exactly one address `start_url`
would accept opens that address instead, the same free public navigation as a `start_url`;
an account run's query is always searched. Open-web pages are always observed in the
real browser (a plain HTTP fetch for simple HTML is deferred, operator ruling 2026-10-21).

**Downloads.** A link handle is fetched by `Downloader.fetch`; an element handle is clicked
while the controller waits up to 30 s for the browser download it starts, which
`capture_browser_download` keeps. A refused hop is `blocked`, never held. A public-session link
download is fetched freely and a type outside the document set is quarantined until the check
lets it through ([`casa-browser-files.md`](casa-browser-files.md)). Archives and file reads run
in a thread.

## Live view (run states)

`request_live(run, purpose)` drains the in-flight work, moves `running` → `waiting_live` and
issues a grant (120 s, `LiveView.issue`). The purpose is `login` or `assist`; a `login` view
needs an account session. Before that transition, a `login` view on a site with login origins
takes the run's session (`_ensure_session`, which reopens one that is gone or stale, as
`begin_live` would) and, when the guest flow has not been started for it, runs it, so the
operator's link goes out with 1Password unlocked for its own fill. A flow that does not complete,
or a K1 under it, lets the request go on (the claim then opens a new session whose guest has not
run); an `assist` view runs no guest flow. `begin_live(run, expose)` — the ingress claim, before the
operator's input is exposed — moves `waiting_live` → `live`; on an account session it begins the
manual lease (`begin_manual(lease, purpose=…)`, keeping the window token), opens a `capture`
driver session and arms the broker's
capture through `broker_client.arm_capture_then`, which calls `expose` only after the capture is
acknowledged; the capture belongs to the view from that acknowledgement on. Any failure while
a view is claimed — in either session kind, at any step (the lease, the capture session, the
capture, the exposure) — leaves through one cleanup (operator ruling 2026-10-21) and offers no
view: the capture the broker armed is ended, the
lease ends, the window closes, the run returns to `running` with a `live_unavailable` event.

**A view nobody opens.** A view `request_live` issued (its `_Live.asked_at`) that is still
`waiting_live` a question's hour later (`confirmations.QUESTION_TTL_S`) cancels its run: the
controller's 30-second sweep (`expire_live`, beside `expire_questions`) records a `live_done`
event with outcome `expired` and ends the run `cancelled` (no notice; the plugin tells whoever
asked). Only `request_live` sets `asked_at`: a view from a question ends with its question, and
a view the operator opened (`live`) stays his until Done.

**The view ends first.** Every path that ends a run or cuts its work — `cancel`, an interruption,
re-pairing, forget, shutdown and every `_end` — ends the live view (`LiveView.revoke`: no more
socket or input) before it waits for anything; the teardown and a refused claim then wait for
the one operation in flight (`LiveView.end`) before the capture, lease and window are released.
Done first closes the view and restores Chrome's window, verified (`LiveView.close_view`); an
unverifiable restore interrupts the run (`window_unrestored`).

Done (one tap; nothing is reviewed): `live_done(run)` ends the broker's capture
(`end_capture`: an unsaved changed value leaves the warning notice; the site's next live view
can capture again; a capture that cannot be ended interrupts the run, whose `end_run` retires
it), the capture session, the manual lease and — after the proxy's remaining events of the view were consumed — the window.
Before that release, a `login` or `assist` view on an account site takes its capture's last
sign-in attempt (`take_attempt`); one that may be saved becomes a `signin` judgement and the
broker holds that capture (`end_capture` keeps it) until the verdict or the run's end
([`casa-browser-credentials.md`](casa-browser-credentials.md)).
A `login` view's operator Done learns at most one provider binding (tags only) — nothing is asked (operator ruling 2026-10-31): exactly one provider attempt and the provider's redirect back to a callback on one of the site's own origins (`login.learn_provider_binding`); a sign-in that failed but was still redirected back teaches it too, an accepted residual; `/v1 …/live-complete` learns none. A learned binding
records the proxy binding key it was learned under (the `binding_key_id` the controller read
from the proxy's `status` at its start; `bindings_lost` names the sites whose record differs
from the current key or is missing) and installs it. A view opened from an open question
(`live_from_question`, [`casa-browser-approvals.md`](casa-browser-approvals.md)) starts from
`waiting_approval` and returns there while the question is still open. Every other Done returns
it to `running`. `POST /v1/control/runs/{run}/live-complete` is the same Done without the operator's page
(no binding); its capture is ended the same way, reviewed first, so its values reach the
broker, which warns if they end unsaved.

## Ends

`finish`, `cancel` and every interruption (a proxy restart, a dependency failure, pairing
revocation, forgetting a granted site) move the run to `finishing` and tear it down in its own
task, in this order: the event pump, in-flight work, the live view (capture
session, manual lease, window), any driver session, the proxy's `revoke`, the browser (closed,
then the launcher's exit proof), the broker's `end_run`, the run's handles.
Each step is bounded and a failure skips none of the later ones; however often the command
that ends the run, or its caller, is cancelled, the teardown runs to its end, and a run that is
already ending is not cut again (a `cancel` during a `finish` returns the run). Every driver
session still open after the exit proof — an unconfirmed close's, or one a failed opening left
behind — is retired then.

**The four holds** (operator ruling 2026-10-21): a run holds exactly four things outside the
controller's memory — the proxy's policy for it, Chrome, the login driver's sessions and (an
account run) the broker's run state with its captures. Its release is confirmed only when the
proxy acknowledged the revoke, the launcher proved no Chrome of the browser user runs, no
driver session remains and the broker acknowledged `end_run`. Confirmed, the run takes its
intended terminal state (`finish` writes its record first: the summary masked again, its
files, their delivery outcomes, its granted sites), gets its event — and, when interrupted, a
durable `run_interrupted` notice — and the next queued run starts. Unconfirmed, the run ends
`interrupted` (never completed or cancelled), its event (outcome `release_unconfirmed`) and
its durable notice tell the requester that the next run waits; the controller keeps the
browser lease and retries the release every 30 s, and no queued run starts until it is
confirmed. An account run whose `end_run` is unacknowledged is also written to the store's
`owed_releases` before it takes its state, so the obligation survives a controller restart.
A run that is ending
never gets another install. `cancel` is idempotent; a queued run is cancelled by its transition
alone. There is no run-duration limit: time is a progress signal (events carry their time).

**Service start.** Before a run is ended or begun: `status()`; a policy a previous controller
left installed is revoked, then the launcher's exit proof — both retried every 5 s until
confirmed (the API and the UI start listening only after this start has finished). Then
every non-terminal run is interrupted (`Store.interrupt_nonterminal`, one durable notice each and, for an account
run, its owed release in the same transaction), ended at the broker (else a stale unreviewed
capture would block that site's next live view) and dropped from memory; every owed release —
this start's and a previous process's — is retried, and while one is owed no queued run is
leased; a retry every 30 s advances the queue once the broker acknowledged them all (the
four-holds rule across a restart). Broker notices are imported; partial downloads removed; the
retention sweep runs then and every 6 h. Nothing resumes.

**Pairing revocation** (`Pairing.arm`): with advancement suspended, every live view ended,
every queued run interrupted, live grants dropped, then the active run is
interrupted — no queued run is leased under the revoked secret. A command already waiting for
the lock when the operator re-pairs is refused (`interrupted`) once it gets it; work in flight
is cut at once, and no new run is admitted (`not_ready`) until the revocation completed.

**Forget** (`POST /v1/control/sites/{site}/forget`): an active run granting the site is
interrupted first; then the launcher deletes the site's profile, the site's provider bindings
and their key record are cleared and the broker's registry entry removed. No vault call.

**Notices.** Broker notices are imported at start and before `GET /v1/notices` answers
(`BrokerClient`); the store's dedupe key makes each import idempotent.

## Residuals (ruled shapes, documented)

Two cases fall under the operator's rulings of 2026-10-21 on live-view cleanup and on the
four holds; each fails closed and is accepted as stated, not patched further:

- A live view whose claim fails while the controller reopens the run's browser (before the
  view's own steps) interrupts the run instead of returning it to `running`.
- A `begin_manual` whose acknowledgement is lost leaves the proxy's credential window open
  (its token never reached the controller) until the next run's first browser start closes
  it. Meanwhile the run's request events are recorded with their origins withheld, as inside
  any credential window — nothing is admitted that would otherwise be refused.

## The service

`svc-browser-controller` runs `python -m casa_browser.controller` as `casa-controller` (uid
1103) with an allowlisted environment, after the floor, the proxy (its exported CA), the broker
and the launcher. It makes `/run/casa-browser/controller` (`0700`) and
`/run/casa-browser/driver` (`0711`, the login driver's socket, peer uid 1102 only), serves the
`/v1` API on port 8098 and is the launcher's one client. The `log_level` option sets the level
of the app's own loggers (`controller.configure_logging`; default `info`); libraries stay at
warnings, and those that can carry request bytes are filtered.

## Invariants

**INV-BRW-024**: A session's policy is installed only after the launcher confirmed that no Chrome of the browser user runs; a run takes its intended terminal state, and the next queued run starts, only after its four holds are released — that proof, the proxy's acknowledged revoke of its policy, no driver session left and (an account run) the broker's acknowledged `end_run` — and a run whose release is unconfirmed ends `interrupted`, saying so, while no queued run starts until the release is confirmed.

**INV-BRW-025**: A run is never resumed or reinstalled after a restart: a proxy restart, a controller restart and every dependency failure interrupt it, and every install names the proxy start it expects, which a restarted proxy refuses.

**INV-BRW-044**: In an account run a navigation to an address off the run's account sites is sent only from the public session, after the account browser closed; a top-level navigation the run's leases-free policy refuses is never sent and is answered `blocked`; and the model never types into a password field.

**INV-BRW-027**: Every driver session the controller opens is closed on every path, and a login or assist live view on an account session exposes the operator's input only after the broker acknowledged its capture (a public-session assist view runs no capture).

**INV-BRW-028**: Only a page of the run's public session, `https` and at most 800 bytes, gets a source id, and its address leaves the app only through the trusted sources route.


## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/controller.py`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-controller/run`

**Tests**
- `casa_browser/tests/test_controller.py`
- `casa_browser/tests/integration/test_run_lifecycle.py`

**Related**
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
- [`reference/casa-browser-files.md`](../reference/casa-browser-files.md)
- [`reference/casa-browser-approvals.md`](../reference/casa-browser-approvals.md)
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
<!-- END SOURCEMAP -->
