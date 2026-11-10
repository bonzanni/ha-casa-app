---
last_reviewed: 2026-11-01
---

# Casa Browser — questions to the operator

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when the action check answers `ask` for an agent's action on a signed-in site:
the question the app freezes (`casa_browser/rootfs/opt/casa_browser/confirmations.py`), its
view, the operator's answer through Casa's trusted callback, the tap-time re-judgement and final
read, the live view from a question and a question's hour. Which actions are checked, the packet
and the verdict routes are [`casa-browser-actions.md`](casa-browser-actions.md); the run states
are [`casa-browser-runs.md`](casa-browser-runs.md). The app asks nothing else of the operator
about a site's requests: there are no request approvals, bundles or demos.

## Mental model

**The operator answers the action, never the request.** A question names the one action the
check asked about, with the check's own words, and only `confirm` can execute it — after the
page is read again and judged again at the tap. Every answer is applied once and its response
stored, so a lost reply is replayed, never re-executed.

## Questions

`ask` creates a `confirmations` row (migration 8: id, run, revision, digest, state, the view,
the stored response; one `open` or `confirming` per run) and moves the run to
`waiting_approval`; the frozen operation stays in controller memory. The view
(`confirmations.py::build_view`) holds the check's text, confirm label, extra options,
highlight strings, the question screenshot's id, `confirm_offered` and the live-only copy; and
`after_lapse` when the action is judged afresh because his Yes lapsed (`controller.py::_lapse`), so
the new card can say his last Yes did not run.
**F6:** a `payment`, a `commitment` the check wants a `required` screenshot for, or a page
holding a payment-card field needs the app's faithful question screenshot
(`observe.py::Observer.capture_question`: secret fields and child frames masked, the target
outlined); without one, no confirm button. **F6a:** `text_incomplete: true` means no confirm
button. **A blind reading** (below) means no confirm button and no plan card: the view keeps the
check's text and adds copy that the app cannot re-check this page, offering the live view
(`confirmations.py::COPY_BLIND`). A plan card's view adds `plan`, verbatim, with no screenshot
([`casa-browser-actions.md`](casa-browser-actions.md)); the plugin posts it in pages above the question. The view never holds an exact destination. `GET /v1/control/runs/{run}/questions/{question}`
returns it.

`POST /v1/control/runs/{run}/questions/{question}/answer` (`answer_question`) is a
compare-and-set on question, revision, digest and state (**F7**): a repeat of an answered
revision returns its stored response with `repeat: true`; a lapsed, superseded, expired or
other-revision tap is `not_current`. `not_now` declines and an extra option answers — neither
executes. The plugin also uses `not_now` to take back a question the operator never saw, with an
`operator_ref` of `fido:<why>`; the event then says so, not that he declined. `confirm` builds a fresh whole reading and the re-judgement input
(`{approved_packet, approved_question, current_packet, difference, destination_changed}`,
`gate.py::difference` over the whole readings, never the capped packets) and answers
`rejudgement_required` (a change is decided on the complete raw values and shown masked and
uncut); a blind reading or a difference over 12,000 characters has no
`same` and lapses, nothing pressed, with its `reason` (`incomplete`, `too_much`) and its own `copy`
saying which: the page cannot be read whole (`confirmations.py::COPY_INCOMPLETE`), or it changed too
much (`confirmations.py::COPY_TOO_MUCH`). A reading only past the control cap is neither. A target that cannot be re-found lapses with `target_gone`;
two controls the reading cannot tell apart count as that too, so its event says so.

A re-judgement's `changed`, no verdict, or `same` with `text_incomplete` or
`destination_changed` lapses the question and judges the action afresh. `same` leads to the
**final read** inside the guard: every block, the text of the target's own record
(`gate.py::target_record`, the same button moved to another row is a difference), every control's
state and every exact destination must equal the reading the re-judgement judged
(`gate.py::same_reading`, no exemption); a difference reruns the re-judgement, at most twice for the
tap, then the question lapses. An
executed tap records `Change("action_confirmed")` and an `action` event carrying the question
screenshot. A run's end expires its questions; a restart marks an open question `expired` and a
`confirming` one `outcome_unknown`.

**A long page, and a blind reading.** A reading lists at most 400 controls a frame, those in view
first, so past that cap its list moves with the viewport. Past the cap a reading also carries
`all_controls`, one value per shown frame in frame order: for a frame that overflowed, a SHA-256,
keyed by the app's salt, of its semantic controls and those it marks with `tabindex` or `onclick`, in
document order, each by role, name, tag, input type, state, link and enclosing link address, and
submit and Enter action, method and target (`gate.py::COMPARED`); for a smaller frame beside it, a digest of
the same facts of its list. A withheld frame stays compared under `frames_withheld`. Past the cap the
final read compares `all_controls` instead of the list, so a stable page's Yes presses however it was
scrolled, and one reading with the value and one without are unequal. In the `difference` equal values
drop every `controls_*` member; unequal ones drop `controls_added` and `controls_removed`, keep
`controls_changed` and add `controls_beyond_list_changed: true` for the check to weigh; code decides nothing on that flag.
A page that fits has no `all_controls` and is compared by its list. A reading short for
any other cause is **blind** (`gate.py::blind`: 5,000 shadow roots, 20,000 blocks, one block over 200,000
characters, an unreadable frame, a closed shadow root that holds a field): it offers no
confirm and has no final read. Two residuals: a control known only by its pointer cursor is not
in the digest (a `:hover` cursor rule changes it as the pointer reaches the target; its text is in the
blocks and it carries no destination), and neither the list nor the digest reads `data-*` attributes.
A `go` under an approved plan is held to `gate.py::same_as_judged` instead, which compares the list.

**Posting.** The plugin's own code renders the question from this view; no model words it. A
question with a screenshot is ONE message: the picture, the check's question as its caption,
and [Yes, confirm] (only when `confirm_offered`), the extra options, [Not now] and
[Open live view] (a link button to the app's page), with fixed labels; the confirm
exists only on that picture's message (a
question re-posted after its picture card was withheld offers no confirm). The page as it
stands after a confirmed step is `POST /v1/control/runs/{run}/screenshots` (a running run, the
page-view gate as for every operator screenshot). An answer is posted with one body: a lost reply is posted
again with the identical body, and the route answers a repeat from its stored response.

**The live view from a question.** `POST /v1/control/runs/{run}/questions/{question}/live`
(`live_from_question`, a control route, `{revision, digest, n, operator_ref}`) opens an
`assist` view from the open question: `waiting_approval` → `waiting_live` and a grant. The
response is stored under question, revision and `n` (G2); a repeat returns it with
`repeat: true`. The app's home page asks the same itself: a run waiting on an open
question that is not a login question is listed under the live view with Open, whose POST — the
operator's own action through ingress — asks this route for that question (its revision and
digest, a fresh `n`) and then opens the view. A view already asked from the question answers `live_open`; a question no
longer open answers `not_current`. At Done, or when no view can be offered, the run returns to
`waiting_approval` while the question is still open (`_after_live`), so its buttons answer as
before. A later confirm is re-judged against the page as the operator left it. If the target is
gone (he submitted it himself), the question lapses and its event tells the model to observe.
While the operator holds the view a confirm is `not_ready`; an answer to a view asked but never
opened ends that view first.

**Login questions.** The app starts two judgements itself, outside any agent command:
`login_item` (a site with no linked login: the site, the sign-in page's real host and each vault
item's index, title and hosts) and `signin` (after a live-view sign-in, see the credentials
reference). `GET /v1/control/runs/{run}/judgements/pending` (`api.py::pending_judgements`)
serves them — a `login_item` only while the run is running — and the plugin's code relays each
through Casa's check to the same verdict route; a run command never supersedes them, and their
item ids never leave the app. A `login_item` verdict of 0–5 of the packet's own indexes asks the
login question (fixed copy: the host, the vault's whole titles, told apart by their hosts when
equal): [Yes] [No] for one, the titles and [None of these] for several, no confirm and no live
view. `{"item": i}` links that item and signs in again; No, None of these, no candidate, no
verdict or the question's hour report `login_needed`. A site that is not set up gets its own question, the sign-in card (view `kind: site_signin`, Yes or No only), in [`casa-browser-signin.md`](casa-browser-signin.md).

**Lifetime.** An open question lives one hour (`confirmations.QUESTION_TTL_S`, its tap tokens'
lifetime). The controller's question sweep (`expire_questions`, every 30 s) then moves it
`open → expired`, with a `question` event, and the run back to `running` (a live view open at
that moment stays the operator's and returns the run to `running` at Done); nothing it froze runs.

## Contracts & invariants

**INV-BRW-037**: A tap executes the frozen operation at most once, and only after a `same` re-judgement and a final read identical to the reading it judged.

**INV-BRW-038**: A question offers a confirm button only when the check stated its facts (`text_incomplete` false), the reading is not blind and, for a money question, the app holds a faithful question screenshot.

**INV-BRW-066**: Past a reading's control cap a stable page's confirm presses however it was scrolled, and a change in any comparable control, listed or not, in any shown frame, or in the text of the target's own record, stops the press.

**INV-BRW-054**: Only `confirm` can execute a question's frozen operation: `not_now` declines and an extra option answers, neither pressing anything, and a tap on a question that is no longer current (another revision or digest, lapsed, expired) executes nothing.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/confirmations.py`
- `casa_browser/rootfs/opt/casa_browser/api.py::answer`
- `casa_browser/rootfs/opt/casa_browser/api.py::question`
- `casa_browser/rootfs/opt/casa_browser/api.py::question_live`

**Tests**
- `casa_browser/tests/test_controller.py`
- `casa_browser/tests/test_gate.py`
- `casa_browser/tests/integration/test_action_gate.py`
- `casa_browser/tests/integration/test_long_page_final_read.py`

**Related**
- [`reference/casa-browser-actions.md`](../reference/casa-browser-actions.md)
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
- [`reference/casa-browser-signin.md`](../reference/casa-browser-signin.md)
- [`reference/casa-browser-contract.md`](../reference/casa-browser-contract.md)
<!-- END SOURCEMAP -->
