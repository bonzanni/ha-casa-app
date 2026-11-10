---
last_reviewed: 2026-11-07
---

# Casa Browser — a run's live-view states

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the run controller (`casa_browser/rootfs/opt/casa_browser/controller.py`) does around a live
view: the states a run moves through, the claim, what any end of a view releases and what Done
does. The rest of the controller is [`casa-browser-runs.md`](casa-browser-runs.md); the ingress
pages and the view's screens, sockets and window are
[`casa-browser-ingress.md`](casa-browser-ingress.md).

## The states

`request_live(run, purpose)` drains the in-flight work, moves `running` → `waiting_live` and
issues a grant (120 s, `LiveView.issue`). The purpose is `login` or `assist`; a `login` view
needs an account session. Before that transition, a `login` view on a site with login origins
takes the run's session (`_ensure_session`, which reopens one that is gone or stale, as
`begin_live` would) and, when the guest flow has not been started for it, runs it, so the
operator's link goes out with 1Password unlocked for its own fill. A flow that does not complete,
or a K1 under it, lets the request go on (on a new session whose guest has not
run); an `assist` view runs no guest flow. A `login` view on `about:blank` first loads, with no
sign-in, the site's page the run was last on (`_Active.page_url`) or its start page
(`_load_for_login`), at the request and again at the claim. `begin_live(run, expose)` — the ingress claim, before the
operator's input is exposed — moves `waiting_live` → `live`; on an account session it begins the
manual lease (`begin_manual(lease, purpose=…)`, keeping the window token), opens a `capture`
driver session and arms the broker's
capture through `broker_client.arm_capture_then`, which calls `expose` only after the capture is
acknowledged; the capture belongs to the view from that acknowledgement on. Each stage is logged
at `info` with its time and nothing of the page. Any failure while
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

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/controller.py`

**Tests**
- `casa_browser/tests/test_controller.py`

**Related**
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
- [`reference/casa-browser-ingress.md`](../reference/casa-browser-ingress.md)
- [`reference/casa-browser-approvals.md`](../reference/casa-browser-approvals.md)
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
<!-- END SOURCEMAP -->
