---
last_reviewed: 2026-10-22
---

# Casa Browser — the protocol v1 contract for Casa's plugin

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a trusted caller — Casa's browsing plugin — can rely on when it talks to the optional
`casa_browser/` app over `/v1`: every member name, bound, error code and event kind as the
code holds them, gathered in one place. The routes, the guard and the error table are
[`casa-browser.md`](casa-browser.md) ("Protocol v1"); runs and their states
[`casa-browser-runs.md`](casa-browser-runs.md); the operator's questions
[`casa-browser-approvals.md`](casa-browser-approvals.md); files and deliveries
[`casa-browser-files.md`](casa-browser-files.md); the ingress pages
[`casa-browser-ingress.md`](casa-browser-ingress.md). `casa_browser/tests/fake_controller.py`
serves every route with the same wire shapes (`api.make_app(FakeController(...))`), so a
caller's contract test runs against it without Chrome, a proxy or 1Password; its methods are
coroutines, like the real controller's.

The app completes only its own part: Telegram presentation, the requester chain, admission
and memory isolation are the caller's and Casa's.

## Envelope

Every route needs `Authorization: Bearer <pairing secret>` (the claim route only inside the
pairing window); every `POST` needs `X-Casa-Browser-Protocol: 1` and a JSON object body with
exactly the members its parser names — an unknown, missing or repeated member, or a value
outside its bound, is `unsupported` before anything happens. A refusal is
`{"error": "<code>"}` with one status per code (`protocol.ERROR_STATUS`): `unauthorized` 401,
`protocol_mismatch` 400, `unsupported` 400, `blocked` 403,
`not_found` 404, `conflict` 409, `stale_handle` 409, `interrupted` 409, `judgement_stale`
409, `digest_mismatch` 409, `file_too_large` 413, `internal` 500, `not_ready` 503. Times are RFC 3339 UTC strings.

## Bounds (`protocol.py`)

| Value | Bound |
|---|---|
| App-issued ids (`run_id`, `handle`, `question_id`, `file_id`, `source_id`, `grant_id`, …) | `[A-Za-z0-9_-]{1,64}` |
| Caller ids (`request_id`, `requester_id`, `delivery_id`, `operator_ref`) | 1–128 printable ASCII, no whitespace |
| `task_kind` / `task_text` / `result_expectation` | 60 / 2000 / 500 characters |
| `search` `query`; `finish` `summary_masked` | 300; 2000 characters |
| `fetch` `url`; `fields`, each path | 2048 characters (`start_url_ok`: 2048 bytes); 1–50, 200 characters |
| an action's `text`; its `plan`; a `read` window | 2000 (32,000 with a line break); 12,000; 30,000 characters |
| `caller_label` (claim) | 64 characters |
| `finish` `file_ids` / `source_ids`; `archive` `file_ids` | at most 200 each |
| Sources per run | at most 200; a page address over 800 bytes gets none |

## Request bodies

| Route | Members |
|---|---|
| `POST /v1/pair/claim` | `caller_label` |
| `POST /v1/control/sites` | `display_name`, `origins`, `login_origins`, `operator_ref`, optional `vault_item_id`, `start_page` |
| `POST /v1/runs` | `request_id`, `requester_id`, `mode` (`account`\|`open_web`), `task_text`, `task_kind`, `sites` (`[{site_id, access: account\|public}]`; an account run names at least one), `result_expectation`, `admission` `{origin_kind: operator_telegram, origin_ref, admitted_spec_digest}`; optional `start_url` (an open-web spec only: one `https` address with a host, at most 2,048 bytes, printable, no whitespace; a digest member only when present) |
| `POST …/observe` | none (`{}`), or `sign_in` (a boolean): the page of an account site is first looked at for its sign-in, and a page with sign-in fields answers `judgement_required` with a `sign_in` judgement, whose verdict's reply is `done` with the `observation`, one with no search ([`casa-browser-signin.md`](casa-browser-signin.md)); and/or `find` (1–200 characters of plain words): what holds them comes with handles wherever it is on the page ([`casa-browser-observations.md`](casa-browser-observations.md)) |
| `…/cancel` | none (`{}`) |
| `POST /v1/requests/cancel` | `request_ids` (1–8 distinct caller ids: every id of one request) |
| `POST …/action` | `kind` (`navigate`\|`click`\|`type`\|`scroll`\|`select`\|`back`), `handle`, `generation`; `text` for `type` only (printable characters and line breaks only: no other control character, no line or paragraph separator; CR and CRLF arrive as LF; a line break only into a `multi-line` field, else `not_run` `one_line`, and never with `enter`, else `not_run` `no_enter`); `enter` (boolean) for `type` only; `choice_handle` for `select` only (an option of the select; an option listed under another select stands for the one option of the same visible name in this select, `controller.py::_own_choice`, and with none or several is refused `unsupported`); `direction` (`up`\|`down`) and `amount` (`step`\|`page`) for `scroll` only; optional `plan` (what the task will still do on this site, in the packet: [`casa-browser-actions.md`](casa-browser-actions.md)); optional `ask_first` (boolean: the step goes to the check even when it would run free) |
| `POST …/look` | none (`{}`), or `handle` (outlined) → `{picture}`: base64 PNG, or null when it cannot be taken; a running run |
| `POST …/read` | optional `offset`, and at most one of `handle` (a text field), `file_id` (a text file of this run) and `rows: true` (the page's rows, one JSON line each, [`casa-browser-observations.md`](casa-browser-observations.md)); none: the page's text → `{text, next_offset, complete, masked}`, `complete` being that no limit cut the text and this window reaches its end, so windows followed from offset 0 by `next_offset` join to the whole; a running run |
| `POST /v1/control/runs/{run}/judgements/{judgement}` | `packet_sha256` (64 lowercase hex) and `verdict` (the check's reply object, untouched), optional `latency_ms`; or `no_verdict: check_unavailable` |
| `POST /v1/control/runs/{run}/questions/{question}/answer` | `revision`, `digest` (the question's packet digest), `choice` (`confirm`\|`not_now`\|`{extra: 0–2}`, or `{item: 0–4}` for a login question), `operator_ref` |
| `POST /v1/control/runs/{run}/questions/{question}/live` | `revision`, `digest`, `n` (the operator's live view from this question, from 1), `operator_ref` → `outcome` `live` (with `grant`), `live_open` or `not_current` |
| `POST …/search` / `…/download` / `…/archive` | `query` / `handle` / `file_ids` |
| `POST …/fetch` | `url`, optional `fields` → a step: `done` with `file`, `url` (the address that answered), `text` (null: not readable text), `next_offset`, `complete`, `masked`, and when they apply `source_id`, `not_json`; or `not_run` `reason` (`bad_address`, `http_<status>`, `blocked`, `not_document`: [`casa-browser-files.md`](casa-browser-files.md)). A running run; `not_ready` during a question |
| `POST …/login-code` | `code` (4–10 of `[A-Za-z0-9-]`), `site_id` (the active listing's `code_site`) → a step: `done` when signed in, else `login_needed` with `reason`; `not_ready` when the run waits for no code from that site. The code is never echoed, stored or logged |
| `POST …/sign-in` | `site_name` (1–60, a label only) → a step: `asked` `question_id` (the sign-in card), `judging` (the vault check, relayed by the plugin, then the card), or with no card `not_configured`, `question_open`, `account_site`, `registered_site` `site`, `not_https`, `page_changed`; `not_ready` while the run is not running |
| `POST …/live` | `purpose` (`login`\|`assist`) |
| `POST /v1/control/runs/{run}/live-complete`, `…/sites/{site}/forget` | `operator_ref` |
| `POST …/finish` | `summary_masked`, `file_ids`, `source_ids` (each one this run issued, else `unsupported`), `expectation_status` (`matched`\|`mismatch`\|`unknown`) |
| `POST /v1/control/files/{file}/delivery` | `requester_id`, `delivery_id`, `outcome` (`deposited`\|`delivered`\|`withheld`\|`handed_off`; a `deposited` attempt may later move to `delivered` or `withheld`) |

## Replies

| Reply | Members |
|---|---|
| hello | `protocol_version` (integer 1), `app_version`, `configured`, `needs_locale`, `language_source` (`option`\|`home_assistant`\|null) |
| claim | `secret`, `protocol_version` |
| a site row | `site_id`, `display_name`, `status` (`ready`\|`needs_login`) |
| a run | `run_id`, `request_id`, `state`, `created_at`, `mode` (201 when new, 200 for a repeated `request_id` with the same members; other members are `conflict`) |
| `ActiveRun` (X8, `GET /v1/runs?state=active` → `{runs}`) | `run_id`, `request_id`, `state`, `created_at`, `pending_question` (the open question's id or null), `code_site` (the site whose sign-in waits for a code it sent, or null), `approved` (the plan the operator approved in this run, or null); every non-terminal run of every requester, FIFO order |
| `Observation` | `page_handle`, `generation`, `text`, `elements: [{handle, role, label_masked, kind?, value_masked?, checked?, found?}]` (`found`: `true`, only on what a search found; `kind`: `time`, `date`, `datetime-local`, `month`, `week`, `range`, `color` or `number`, only for such a field; `value_masked`, what an ordinary field holds, masked and bounded, and `checked`, a checkbox's or radio's state — neither for a password, one-time-code or payment-card field), `source_id` (X9: a run-scoped id for an `https` page of the public session, else null), `notice` (the sign-in step a look's sign-in recorded, else null), `text_cut` (`true`, only when the page holds more text than `text`: `POST …/read` has it), `controls_left` (only when the cap left controls or options out: how many), `find` (only with a search: `{whole, left}`, whether all of the page was searched and how many that hold the words did not fit) |
| an action result, a step | `outcome`, `generation`, and per outcome: `judgement_required`/`rejudgement_required` `judgement: {id, kind: action\|rejudge, packet}` (the packet's canonical JSON text, relayed untouched); `asked` `question_id`; a login question's `linked` `label`, `not_linked`; a sign-in card's `signing_in`/`not_opened`/`already_saved` `label`, `not_saved`, `declined`; an app-started judgement's or a code's `login_needed` (a code's carries `reason`), `credential_saved`/`credential_not_saved` `site`, `not_saved`; `look_first` `note`; `not_run` `reason` (for a typing step, nothing judged: `not_typable`, `value_format`, `no_enter`, `one_line`; for a target that cannot be pressed, nothing sent: `not_visible`, `disabled`, `no_size`); `done` after a `scroll` `position: {top, end, moved}` (how far down what it scrolled is, of how far it can go, in CSS pixels, and whether that moved), after a verdict `file` (a download) or `observation` (a search, but none for an address on a site with account access: observe next), and after a judged `type` without Enter `typed` (`shown`\|`not_shown`: whether the field holds the text afterwards, never the value); `answered` `label`; `lapsed` `reason`, maybe `copy`; also `check_unavailable`, `outcome_unknown`, `declined`, `not_current`. A replayed verdict or answer adds `repeat: true`. A checked `action`, `download` or `search` answers `judgement_required` instead of its old reply; a same-host `navigate` on an account session is not checked and answers `done` directly, with no observation (an address `search` too: observe next); a click on a link to a site with account access always goes to the check |
| a question (`GET /v1/control/runs/{run}/questions/{question}`) | `question_id`, `run_id`, `revision`, `digest`, `state`, `view: {text, confirm_label, extra_options, highlight, screenshot_id, confirm_offered, live_only_copy, category, plan?, after_lapse?}` (`plan`: a plan card's plan, verbatim; `after_lapse`: true on a question that follows a lapsed Yes); a login question's view adds `kind: login_item`, `host` and `options` (1–5 titles); a sign-in card's adds `kind: site_signin`, `host` (from the browser), `name` and `item` (the one fitting title, or null), answered `confirm` or `not_now` |
| pending judgements (`GET /v1/control/runs/{run}/judgements/pending`) | `judgements: [{id, kind: login_item\|signin, packet}]`: the judgements the app started itself, for the plugin to relay (a `login_item` only while the run is running) |
| `FileRecord` | `file_id`, `run_id`, `requester_id`, `safe_name` (may be `download.<ext>`), `mime`, `size`, `sha256`, `source_handle` |
| the live-view envelope (`POST …/live`, 201) | `grant_id`, `expires_at`, `operator_delivery_path` (the grant is the operator's; the caller never needs the path: the app's home lists every waiting live view first, X3) |
| a record (`GET /v1/records?requester_id=&since=` → `{records}`) | `run_id`, `requester_id`, `sites`, `summary_masked`, `changes`, `file_ids`, `delivery_outcomes: [{file_id, delivery_id, outcome}]`, `ended_at` — the requester's own runs only |
| a notice (`GET /v1/notices?after=N` → `{notices}`) | `seq`, `kind`, `run_id`, `site_id`, `text_masked`, `at` |
| a source (X9, `GET /v1/control/runs/{run}/sources` → `{sources}`) | `source_id`, `url`, `title_masked` — the one reply that carries an address; never a model tool |
| a judgement's picture (`GET /v1/control/runs/{run}/judgements/{judgement}/picture`) | the masked PNG of the run's open judgement, or `not_found` once its verdict started applying, when it has none, and for a re-judgement; never on a model route |
| a screenshot (X7, `GET /v1/control/runs/{run}/screenshots/{screenshot}`) | the masked PNG, that run's ids only; never on a model route; `POST /v1/control/runs/{run}/screenshots` (no members, a running run) takes one of the page as it stands → `{screenshot_id}` |
| forget | `{site_id, status: forgotten}` |
| a request cancel (`POST /v1/requests/cancel`) | `requests`: one per id asked, in order — `request_id`, `run_id` (null when no run of it exists yet), `stopped` (whether the id is recorded as stopped after the call: the same on a repeat), `state` (the run's, or `cancelled` with no run) — [`casa-browser.md`](casa-browser.md) |

**Admission digest (X4).** `admitted_spec_digest` is `sha256:` + the hex SHA-256 of the RFC
8785 canonical JSON of the run body's members other than `admission`; the shared vector file
is `casa_browser/tests/vectors/spec_digest.json`.

## Events (`GET /v1/runs/{run}/events?after=N`)

Each event is `{seq, run_id, kind, origin, handle, text_masked, outcome, screenshot_id, at}`; `text_masked` is masked and never an address, except that a step to an address the model itself gave (`search`) names that address's host and masked path.

| `kind` | `outcome` | Notes |
|---|---|---|
| `run_queued`, `run_started` | `queued`, `running` | |
| `action` | `done`, `confirmed`, `outcome_unknown` | a screenshot id only after a checked press or Enter; a confirmed tap carries the question's screenshot; `outcome_unknown` may have run and is never retried |
| `judgement` | the verdict, or `no_verdict` (`items` for a `login_item`) | the check's category, latency and masked one-line reason; for `login_item`, `signin` and `sign_in` only the outcome and latency |
| `question` | `asked`, `declined`, `answered`, `lapsed`, `expired` | the run waits in `waiting_approval` while one is open (an hour at most) |
| `page_unreachable` | `blocked` | a navigation the policy would not admit |
| `file_saved` | `done` | |
| `login_needed` | the broker's fixed reason code — for example `not_found`, `password_change_page`, `rejected`, `challenge`, `page_unsettled` (the page kept changing, or the fields it showed went away, during the sign-in), `needs_code`, `code_page_gone`; `unknown` for a code that is not short ASCII | the run waits for a `login` live view; no automatic password reset; `needs_code`: the site sent a code, which `POST …/login-code` takes |
| `login_code` | `submitted`, `needs_code`, `not_signed_in` | a code was given for the site the text names; never the code |
| `login_item_judgement`, `signin_judgement` | `pending` | a judgement the app started (a site with no linked login; a live-view sign-in at Done) waits for the plugin's relay |
| `sign_in_judgement` | `pending` | a look's `sign_in` judgement, returned in the observe reply |
| `sign_in_sent` | `submitted` | Casa Browser filled in and sent a sign-in on a page off the start page; no claim that it worked, the model reads the page |
| `sign_in_nothing` | `no_form`, `not_checked`, `page_moved` | a registered site's sign-in sent nothing: its start page showed no form while it waited, a look found sign-in fields it could not bring to the check (a reading over its caps, a closed shadow root holding a field, a page that moved), or the judged fields or button moved before the send. The next observe still looks at a form that rendered late |
| `signin` | `not_saved` | a live-view sign-in not judged (no attempt that could be saved) |
| `credential_saved`, `credential_not_saved` | `saved`, `item_kept` | the live-view login saved; or a working linked item left as it is |
| `live_requested` | the purpose | |
| `live_unavailable` | a short code | the run is back in `running` |
| `live_done` | `running`, or `waiting_approval` back to the open question it was opened from | |
| `run_finished` | `completed` | |
| `run_failed`, `run_cancelled` | `failed`, `cancelled` | a run created for a request already stopped has `run_cancelled` alone |
| `run_interrupted` | why — for example `app_restart`, `proxy_restarted`, `site_forgotten`, `pairing_revoked`, `release_unconfirmed` | with a durable `run_interrupted` notice |

## Locale

The app's language is Home Assistant's configured language, read by root bootstrap at every
start, unless the `language` option overrides it; the time zone is Supervisor's `TZ` unless
the `timezone` option does. Without one of them `hello` reports `needs_locale: true` and new
runs are held. The ingress pages show the operator's text in that language when the app has
a catalog for it (English, Italian, Dutch, German), else in English
([`casa-browser-ingress.md`](casa-browser-ingress.md)); the wire carries no translated text.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/tests/fake_controller.py`
- `casa_browser/tests/vectors/spec_digest.json`

**Tests**
- `casa_browser/tests/test_protocol.py`
- `casa_browser/tests/test_api_auth.py`
- `casa_browser/tests/integration/test_adversarial_sites.py`

**Related**
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
- [`reference/casa-browser-approvals.md`](../reference/casa-browser-approvals.md)
- [`reference/casa-browser-files.md`](../reference/casa-browser-files.md)
- [`reference/casa-browser-ingress.md`](../reference/casa-browser-ingress.md)
<!-- END SOURCEMAP -->
