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
[`casa-browser-runs.md`](casa-browser-runs.md); the pacer
[`casa-browser-browser.md`](casa-browser-browser.md); observations
[`casa-browser-observations.md`](casa-browser-observations.md); wire members
[`casa-browser-contract.md`](casa-browser-contract.md).

## Mental model

An in-scope action that can send does not run when the model asks for it. The controller reads the page
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
download is made, and on an account session whether the destination stays on the host open now
(below). `scroll` is free everywhere. In the public session a navigation, a link
click, `back`, a search, typing into a search field and a link download are free; a free link
download whose file is not a document is judged after the fetch, before it is kept (kind
`download`, the packet's `downloaded_file`; [`casa-browser-files.md`](casa-browser-files.md)).
Everything else is checked, except what sends nothing by itself. The broker's own login is not an agent action and keeps its own
guard.

**Sends nothing by itself** (`gate.py::sends_nothing`, in `RunController._prepare`), on signed-in and
public sites alike: typing without Enter (a line break too, as text into a `<textarea>`), choosing a
select's value, ticking a checkbox or radio (`role`), opening a disclosure (a `summary`, a button
with `aria-expanded`), none of them with a destination (`gate.py::exact_destination`: not a link, not a
submitter, not inside one) and none secret. These run with no check, under a go's own guards (F4, F9, the
keyboard rules, the select's one in-page step). A press or an Enter that sends is judged as ever: its
reading holds every tick and value made so far (`target.form.fields`, `target.submission`,
`page.controls`), and money asks there. A step that carries a `plan` goes to the check, so its Yes can
be asked, and so does one Fido marks `ask_first`: a tick, choice or typing whose label says it commits
money now or later (an auto-renewal, an automatic top-up, a paid plan), which an honest page may save at
once with no press to check (operator ruling 2026-11-09; sites are assumed truthful, and noticing it is
Fido's judgement). Tabs, menu items, switches and any other button stay checked.

**What this leaves.** A page's own script that sends when a box is ticked, a value chosen, a menu
opened or a key typed (an autosaving setting, a form that sends as you type, a button marked
`aria-expanded` that acts) now runs without a question: no code can tell it from a page that waits for
the press. This is the class of operator ruling 11 (a saved sort). Fido's doctrine asks him to touch a
box or field a page says saves itself only when the task asks for that change.

**An address on the host open** (`gate.py::in_scope`'s `same_host`, `RunController._free_on_host`): on
an account session a `navigate`, or a search that is exactly one address, runs with no check when the
destination loads another page of the same host as the page open now. Each opens an address and presses
nothing, so nothing the page put on a link runs. The facts are mechanical: a web page is open and the
destination has its hostname (`gate.py::same_host_load`), the destination belongs to this session's own
site (`RunController._on_host`: no session switch), the step carries no `plan`, and a link's address has
not moved since it was observed. Everything else stays checked: a subdomain and any other host, even of
the same site, no page open yet, the open page's own address with a fragment (it
loads nothing, it only tells the page's script), a click, `back`. **A click on a link to a site with
account access always goes to the check**: it is a real press, a script behind a link is an action, and
the link's visible name is what the check judges. (A link to a public address needs the public session,
so it loads as a free navigation there and presses nothing.) A search's `done` carries no observation here either, and the
plugin observes. The sign-in lease, the network floor and the policy's refusals do not change.

**What this leaves.** An address the agent itself opens on the same host runs without a question, and a GET
to it that changes something by itself (an address that unsubscribes, confirms or deletes when it is
loaded) is not caught. This is an accepted residual, the same class as above, and it is what the speed
of a long job is traded for: such a load's check saw only the origin and masked path of the address, on
the page being left.

**The free inputs are a closed list, each checked at delivery** (G1): a free navigation runs
only if the session at the load is the public one (`_free_nav_guard`, before `_goto`); a free
link click only while it is still an http(s) link to the public session and the point lands
on it; a same-host navigation only if the facts above still hold just before the load; `back` only in
the public session; free typing only while the field is still a search
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
`page_truncated`), `page.controls` (role, masked name and state of every control the reading
lists, at most 400 a frame; what a tap compares past that is in
[`casa-browser-approvals.md`](casa-browser-approvals.md)), and `run_so_far` (the last three judged actions). Text passes `masking.mask_text`.
**Every field's value is shown whole, once** (`gate.py::in_target_form`): masked and never cut, in
`target.form.fields` when the target's form lists the field, else in the field's own `page.controls`
line (a field with no form owner, another form's, a contenteditable). Elsewhere a value shows its
first 200 characters: the control line of a field the form lists, `target.state`, the submission's
entries. So a long list typed into a field reaches the check entire, whatever sends it. Casa's
check takes a packet of at most 512 KiB (`action_judgement.py::MAX_PACKET_BYTES`); a larger one
gets no verdict, and the step runs nothing.
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

A look's `sign_in` packet (`gate.py::sign_in_packet`) reuses this page part — title, masked
path, numbered masked blocks, control lines — beside the site and `planned` (what a sign-in
there would fill and press); its reply bound is `gate.py::sign_in_within_bounds`.
`PageOps.read_page_and_bound` reads the page once and gives the whole reading and, from the same
frame readings, the main frame and the look's bound frames only: only those must read whole for
the look to be judged ([`casa-browser-signin.md`](casa-browser-signin.md)).

**The picture.** `_freeze` first takes the page's picture (`Observer.capture_for_model`, the target
outlined, at most 12 s and 2 MiB), then reads, so what its scroll loaded is in the packet and a move it
caused is stale. It stays in memory on the pending judgement, outside the packet and its digest;
`GET /v1/control/runs/{run}/judgements/{judgement}/picture` hands it to the plugin's relay until the
verdict starts applying. Any failure is no picture, and the check runs on the text.

**A plan and its approval.** `run_act`'s `plan` (what the task will still do on this site, list
included) is a packet member, and its Yes approves the whole of it: `ask` with category
`account_change`, a plan, a confirm button and no money (`confirmations.py::money`) is a **plan card**,
`view["plan"]` verbatim and no screenshot. Only an executed confirm on a plan card keeps
`_Active.approved`, bound by `RunController._site_key` to the site's id (else the page's origin), never
a display name. A later packet on that site carries it as `approved` unless the page has a card field,
and only then may a `go` be `account_change`; such a go has no Enter after typing, runs only while
all the judged packet was built from is unchanged (`gate.py::same_as_judged`: text, controls, title,
address, card fields; else it is judged afresh, so a fee shown since is never pressed on the earlier
Yes), and records a `Change`. A `payment` or a
`commitment` always asks, and its Yes changes nothing here. Casa adds `rulebooks/scope_v1.txt` when a
packet carries either member. No code compares a list with the approved one: Fido's final read-back
does, and says what differs.

## Verdicts

`POST /v1/control/runs/{run}/judgements/{judgement}` (`RunController.apply_judgement`) takes
`{"packet_sha256", "verdict"}` or `{"no_verdict": "check_unavailable"}`. In order:

1. A stored id answers its stored response with `repeat: true`; otherwise the id must be the
   run's current open pending judgement (one per run, superseded by any new command, 180 s),
   else `judgement_stale`.
2. The verdict's digest must equal the frozen packet's, else `digest_mismatch`; nothing runs.
3. **F1.** No verdict, or one outside the check's reply bounds (`gate.py::verdict_within_bounds`,
   the same bounds as Casa's), is `check_unavailable`; the plugin has already retried once.
4. **F2.** A `go` whose category is not `reading` or `local` (`gate.py::go_allowed`), or
   `account_change` on a packet carrying `approved`, becomes a question with fixed copy and no
   confirm button.
5. `look_first` returns the masked note; `ask` freezes a question; `go` executes.

The id moves `open → applying` before any dispatch, and the complete response — including a
successor (`judgement_required` judged afresh, `rejudgement_required`) — is stored in the
`responses` table (migration 8) before the pending judgement is replaced.

**Executing a go (F4, F9).** Immediately before the dispatching input the controller re-reads
the target's own facts (role, name, form identity and field values, state, exact
destination) in the same browser session; a target inside a link, a submit button or a label of a
submit input has the facts of the element its press activates (`READ`'s `activatorOf`: an enclosing
link or submit button wins over a nearer control, since Chromium hands a click on from a type=button
or a text field; the link's address, the submitter's submission); the target resolves in the same page generation, or
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

**How a `type` reaches its field** (a value field's one in-page step, the keyboard rules, line breaks)
is [`casa-browser-browser.md`](casa-browser-browser.md).

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

**INV-BRW-062**: A `go` on `account_change` runs only on a packet carrying `approved`; `approved` is set only by an executed confirm on a question that showed the plan, and is absent on another site and on a page with a card field; such a `go` presses only while what its packet was built from is unchanged; `payment` and `commitment` always ask.

**INV-BRW-051**: A site's sign-in code is filled only into the one code field of the very document that reported `needs_code` (the same page generation and the mark the report set in that document, on a login origin, with exactly that field and a submit), once per report, through a fresh login driver session closed on every path; it never appears in a broker or API reply, an error, the run's events, the broker's journal or registry, the app's records, or a log line.

**INV-BRW-036**: An in-scope action runs only after a verdict whose digest is its own frozen packet's; no verdict, a verdict outside the reply bounds, another digest or a stale id runs nothing.

**INV-BRW-039**: A judged press happens only after the target-fact re-read and the hit test pass, immediately before `mouse.down()`; a failure presses nothing.

**INV-BRW-052**: A line break an agent types reaches only a real `<textarea>`, never with Enter after it, and only as inserted text, never as a key.

**INV-BRW-065**: On an account session a navigation to a site with account access runs with no check only when the destination has the hostname of the web page open now and belongs to this session's own site, the step carries no plan, and it is not the open page's own address with a fragment; those facts are asked again just before the load, and every click on a link to such a site is checked.

**INV-BRW-069**: A packet shows every field's value whole and masked, never cut: in `target.form.fields` when the target's form lists that field, else in the field's `page.controls` line (a typed target repeats its own value in the action and its state); a field of another frame is never taken for one of the target's form.

**INV-BRW-063**: Apart from `scroll` and a same-host navigation (INV-BRW-065), an action on a signed-in site that runs with no check is only typing without Enter, a select's value, or a click on a non-secret checkbox, radio, `summary` or `aria-expanded` button with no link or submitter destination; a press or Enter that sends after it is judged on a reading holding every tick and value it made; an action marked `ask_first` never runs with no check.

What these do not cover: whether the check's verdict is right (the model judges, measured
before enabling); a page's own script that reacts to typing, a tick, a chosen value or an opened menu (above); an address the agent itself opens on the same host, whose GET changes something by itself (above); a page that changes in the milliseconds between the last read and the input
(operator ruling 9); Chrome's own network resend of a navigation whose connection closed before
any response byte (an accepted residual, the operator's own browser does the same).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/gate.py`
- `casa_browser/rootfs/opt/casa_browser/api.py::judgement`
- `casa_browser/rootfs/opt/casa_browser/api.py::pending_judgements`
- `casa_browser/rootfs/opt/casa_browser/api.py::judgement_picture`
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
