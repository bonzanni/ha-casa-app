---
last_reviewed: 2026-10-29
---

# Casa Browser — the action gate: checked actions, verdicts and questions

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the optional `casa_browser/` app decides whether an agent's browsing action runs: which
actions are checked, what the check is shown (the packet), how a verdict is bound to that
packet and applied once, what the app guarantees whatever the check says (F1–F9), and the
operator's question with its tap-time re-judgement. The check itself runs in Casa
(`casa/rootfs/opt/casa/action_judgement.py`, see
[`../architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)); the
plugin's own code relays packets and verdicts. Runs and their states are
[`casa-browser-runs.md`](casa-browser-runs.md); the pacer and observations
[`casa-browser-browser.md`](casa-browser-browser.md); wire members
[`casa-browser-contract.md`](casa-browser-contract.md).

## Mental model

An in-scope action does not run when the model asks for it. The controller reads the page
(`gate.py::READ`), builds a packet, freezes the action as the run's one **pending judgement**
and answers `judgement_required` with the packet's canonical text. The plugin's code posts
that text to Casa's check and posts the verdict back to a **control** route bound to the
packet's SHA-256; no model tool reaches either route. `go` runs the frozen action under a
last-moment re-read; `ask` freezes a **question** for the operator; `look_first` returns the
check's note. A tap on a question re-reads the whole page and the check judges again before
anything is pressed. Every verdict and answer is applied once, and its complete response is
stored, so a lost response is replayed, never re-executed.

## Scope of the check (design §3)

`gate.py::in_scope(session, action, …)` over mechanical facts only: the session the input is
delivered in (for a navigation, a link click and a search, the destination's session,
`controller.py::RunController._dest_kind`), the action kind, whether the target is an http(s)
link, whether it is a search field (role `searchbox` or `<input type=search>`) and how a
download is made. `scroll` is free everywhere. In the public session a navigation, a link
click, `back`, a search, typing into a search field and a link download are free; a free link
download whose file is not a document is judged after the fetch, before it is kept (kind
`download`, the packet's `downloaded_file`; [`casa-browser-files.md`](casa-browser-files.md)).
Everything else is checked. The broker's own login is not an agent action and keeps its own
guard.

**The free inputs are a closed list, each checked at delivery** (G1): a free navigation runs
only if the session at the load is the public one (`_free_nav_guard`, before `_goto`); a free
link click only while it is still an http(s) link to the public session and the point lands
on it; `back` only in the public session; free typing only while the field is still a search
field, under the keyboard rules below; `scroll` never wheels over a form control (the pacer checks before every step, see
[`casa-browser-browser.md`](casa-browser-browser.md)).

## The packet

`gate.py::build_packet` builds exactly the measured members: `task` (the run's admitted task
text), `where` (`signed_in`/`public`, site name, host, masked title, masked path), `action`
(kind; `text` and `then_enter` for `type`; the option's label for `select`), `target` (block,
`record_blocks`, role, masked name, tag, input type, link destination, form facts, the
submitter's and Enter's destinations, nearest heading), `page.blocks` (numbered, the target's
own block first, then **the other blocks of its record** — table row, list item or article —
then its form or landmark, then the rest by distance, capped at 12,000 characters with
`page_truncated`), `page.controls` (role, masked name and state of every control on the
page), and `run_so_far` (the last three judged actions). Text passes `masking.mask_text`.
**One secret rule** (`gate.py::SECRET_JS`, spliced into every page script and the question
screenshot's mask): an element typed as a password, or whose `autocomplete` names a password
(`current-password`, `new-password`), a one-time code or a payment
card, and everything inside it (descendants and the open shadow trees they host), is secret.
Every value or label read from an element passes one function (`valueOf`), which shows
`[not shown]` when the element is secret, sits inside a secret element or contains one —
states (a checkbox's or radio's included; a secret element's ARIA states are left out), a
contenteditable's text, a select's options, a submitter's label; block texts and
control names skip text inside a secret element. Every state text and a chosen option's label are
masked; the agent never types into such a field or chooses in a card select. The reading is the whole page
(`gate.py::PageOps.read_page`): the target's frame first, then every other frame, frames read
parents first — the page's own frame when the page-view gate admits its address before and
after the read, every child frame by the one frame rule the observation uses
(`observe.py::frame_shown`: a shown parent and an origin that is not opaque, then a frame with a
web address when the gate admits the address its own reading reports, the one it had before the
read; a frame without one when its reported origin is its parent's). A frame that fails the rule is a placeholder block
whose content is compared by digest; a target in such a frame is not read at all. A destination is
origin plus masked path: the **exact destination** (a link's address, a submitter's
effective `formaction`/form owner destination, a text field's Enter destination) stays
app-private.

**The submission** a press or an Enter would send is read as the browser builds it
(`READ`'s `submissionOf`): method, resolved action, enctype, target, the submitter, and the
entries of `new FormData(form, submitter)`. Which submitter an Enter uses follows implicit
submission: the form's default button in the form's own tree (a disabled one: nothing is
sent), else the form itself when at most one field blocks implicit submission; any input type
whose Enter submits is covered. A form invalid under native validation submits nothing. In a
form that holds a secret, or a custom form control, the entries show names only. Every
comparison binds `raw`, a SHA-256 of the whole unmasked submission and the encoding's inputs
(the `accept-charset` text and the document's encoding), keyed by a per-process salt, so a
change hidden by masking is still a change (`masked_text_changed` in the difference); the
digest is never in a packet. A form whose file input holds a file is not judged
(`unsupported`). An image submitter's `x`/`y` are shown as FormData gives them (`0`); a click
sends its press point (a pixel, not an account fact). The submission is in the packet (`target.submission`,
`target.enter_submission`), in the frozen target facts the last moment re-reads, in the final
read and in the difference.

The packet travels as canonical JSON text (`gate.py::canonical`: sorted keys,
no ASCII escaping, compact) and is bound by `gate.py::digest`.

## Verdicts

`POST /v1/control/runs/{run}/judgements/{judgement}` (`RunController.apply_judgement`) takes
`{"packet_sha256", "verdict"}` or `{"no_verdict": "check_unavailable"}`. In order:

1. A stored id answers its stored response with `repeat: true`; otherwise the id must be the
   run's current open pending judgement (one per run, superseded by any new command, 180 s),
   else `judgement_stale`.
2. The verdict's digest must equal the frozen packet's, else `digest_mismatch`; nothing runs.
3. **F1.** No verdict, or one outside the check's reply bounds (`gate.py::verdict_within_bounds`,
   the same bounds as Casa's), is `check_unavailable`; the plugin has already retried once.
4. **F2.** A `go` whose category is not `reading` or `local` becomes a question with fixed copy
   and no confirm button.
5. `look_first` returns the masked note; `ask` freezes a question; `go` executes.

The id moves `open → applying` before any dispatch, and the complete response — including a
successor (`judgement_required` judged afresh, `rejudgement_required`) — is stored in the
`responses` table (migration 8) before the pending judgement is replaced.

**Executing a go (F4, F9).** Immediately before the dispatching input the controller re-reads
the target's own facts (role, name, form identity and field values, state, exact
destination) in the same browser session; a target inside a link, a button or a label of a
submit input has the facts of the element its press activates (`READ`'s `activatorOf`: the
link's address, the submitter's submission); the target resolves in the same page generation, or
by design §2.6's re-resolution (`_current_target`: same address, exactly one control on the whole
page, in a frame at the target's own frame address, with the frozen role, name, form identity and
exact destination). Submission destinations are the browser's own (the form and submitter IDL
values; the default button found in the form's own tree). For a press the re-read and a hit test
(`gate.py::PageOps.hit`: the browser's own answer, CDP `DOM.getNodeForLocation` at the press
point, level by level through out-of-process frames, must be the target or inside it, and not
inside a link, button or other activatable element within the target, compared as nodes in a
fresh isolated world with nothing written to the page) run inside the pacer's guard after the pointer moved. `changed` runs nothing and judges afresh;
`moved` runs nothing (`not_run`). `select` is one in-page step that checks the select and the
option and sets it in one task (`gate.py::_SELECT_ONE`); no library call waits or retries.

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
payment-card field (refused before any packet); its
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
own checked step); free typing (a public search field) takes none (`one_line`), and the pacer
refuses a line break its caller did not allow before anything is clicked. For a `type` that ends in Enter the dispatch
is the Enter: after a go, the field's facts are read again with the value read back after the
last keystroke; after a tap, the typed page is re-judged first. A judged `type` without Enter
reads the field back after the last key and says only whether it shows the text (`typed`), so a
field that refused the keys is not reported as filled.

**The dispatch journal (G2).** Immediately before every dispatching input — `mouse.down()`, the
Enter, the selection step, `go_back`, a judged navigation's `_goto`, a judged button download's
press, a judged link download's first request — the operation's response is stored
provisionally as `outcome_unknown`. Any end without a recorded result (a timeout, a cut by a
cancel, revocation or drain, the browser gone) keeps it, adds a `Change("outcome_unknown")` and
an `action` event, and nothing retries it (**F8**); a repeat of the verdict while the dispatch is still running waits for its real response. The downloader sends a judged request once:
aiohttp's own resend is disabled (`downloads.py::Downloader._fetch_http`).

## Questions

An `ask` verdict freezes a question for the operator; its view, its answers, the tap-time
re-judgement, the live view from a question and its lifetime are
[`casa-browser-approvals.md`](casa-browser-approvals.md).

## Sign-in codes

A code the site sends is not a checked step: no page control the check judges is touched, and the
broker, not the agent, fills it. An authenticator code (TOTP) in the item is read and filled at
use by the broker's login ([`casa-browser-credentials.md`](casa-browser-credentials.md)). A login step whose only role is `one_time_code`, with a submit,
when the item holds no authenticator, stops before anything is filled on that page: the broker
marks that page's own document through the still-open driver session (`login.py::mark_document`,
a value set in Patchright's isolated world, which the page's scripts can neither see nor set, and
which another tab, a reload or a navigation does not hold), keeps the mark in memory per run and
site, and replies `needs_code` (`code_field`, `totp_held`, and the page's `page_generation`); the
page stays as it is for a code the site sent (by email or SMS). A page that cannot be marked stays
`missing_role`. A code asked together with other fields stays
`missing_role`. The broker's `fill_code` takes that generation and the code (4–10 of
`[A-Za-z0-9-]`, refused `unsupported` otherwise): with the same guest and item checks as
`login`, it uses the report's mark up (no mark: `code_page_gone`; a new login of the site and the run's
end drop it too), and its first step requires the page to still have that generation and that
mark (`login.py::document_marked`; otherwise `code_page_gone`), a login origin, exactly the one
code field and a submit; it fills the code, submits, and goes on with the item's values for any later step (a
further code step is `needs_code` again; a code shown back as invalid is `rejected`, which never
marks the item's password rejected). The code is filled once and kept nowhere: not in a reply,
the journal, the registry or a log line.

The route is `POST /v1/runs/{run}/login-code` (`api.py::login_code`, a normal run route: the model
supplies the code, design §5.4); the controller's part is in
[`casa-browser-runs.md`](casa-browser-runs.md) (Login).

## Contracts & invariants

**INV-BRW-051**: A site's sign-in code is filled only into the one code field of the very document that reported `needs_code` (the same page generation and the mark the report set in that document, on a login origin, with exactly that field and a submit), once per report, through a fresh login driver session closed on every path; it never appears in a broker or API reply, an error, the run's events, the broker's journal or registry, the app's records, or a log line.

**INV-BRW-036**: An in-scope action runs only after a verdict whose digest is its own frozen packet's; no verdict, a verdict outside the reply bounds, another digest or a stale id runs nothing.

**INV-BRW-039**: A judged press happens only after the target-fact re-read and the hit test pass, immediately before `mouse.down()`; a failure presses nothing.

**INV-BRW-052**: A line break an agent types reaches only a real `<textarea>`, only after a verdict on the packet holding the whole text, never with Enter after it, and only as inserted text, never as a key.

What these do not cover: whether the check's verdict is right (the model judges, measured
before enabling); a page's own script that sends when a textarea's value gains a line break
(what a page does in response to typing is part of the action, as for any keystroke); a page that changes in the milliseconds between the last read and the input
(operator ruling 9); Chrome's own network resend of a navigation whose connection closed before
any response byte (an accepted residual, the operator's own browser does the same).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/gate.py`
- `casa_browser/rootfs/opt/casa_browser/api.py::judgement`
- `casa_browser/rootfs/opt/casa_browser/api.py::pending_judgements`
- `casa_browser/rootfs/opt/casa_browser/api.py::login_code`

**Tests**
- `casa_browser/tests/test_gate.py`
- `casa_browser/tests/test_pacing.py`
- `casa_browser/tests/integration/test_action_gate.py`
- `casa_browser/tests/test_broker.py`
- `casa_browser/tests/integration/test_login.py`

**Related**
- [`reference/casa-browser-approvals.md`](../reference/casa-browser-approvals.md)
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
- [`reference/casa-browser-contract.md`](../reference/casa-browser-contract.md)
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
<!-- END SOURCEMAP -->
