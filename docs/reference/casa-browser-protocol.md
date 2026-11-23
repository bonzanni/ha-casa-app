---
last_reviewed: 2026-11-23
---

# Casa Browser — protocol v1 and pairing

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The versioned `/v1` HTTP contract of the optional `casa_browser/` app: its one application constructor, the guard every route passes, the refusal codes, the routes, the request cancel, the admission digest and pairing. The app's options, bootstrap, durable state and request policy are [`casa-browser.md`](casa-browser.md); every member name, bound and event kind is gathered in [`casa-browser-contract.md`](casa-browser-contract.md).

## Protocol v1

Checked actions, verdicts and questions: [`casa-browser-actions.md`](casa-browser-actions.md).

`api.make_app(controller)` is the one application constructor; its handlers await the
controller (every `api.Controller` method is a coroutine — `controller.RunController`, see
[`casa-browser-runs.md`](casa-browser-runs.md)). It listens on the internal
port 8098 (`api.serve`, access logging off). Wire types and body parsers are in
`protocol.py`; a body with an unknown or missing member, or a value outside its bound, is
`unsupported` before anything happens. App-issued ids match `[A-Za-z0-9_-]{1,64}`;
caller-supplied identifiers are 1–128 printable ASCII characters without whitespace.

The guard reads a `POST` body completely before it checks the secret, so a request still
arriving when the operator re-pairs is judged against the new secret. A body that cannot be read
(too large, an undecodable content encoding) is refused after the secret and protocol checks
and its connection closed; an `Expect` value other than `100-continue` is ignored; a JSON
body with a repeated member is `unsupported`. The pairing window exempts only the claim route
reached by its literal, unencoded path.

A request that is not well-formed HTTP (an illegal byte in a header, an unknown method token,
an oversized header line) is answered by the HTTP server library itself, before the guard and
outside this contract (a plain-text 400). `api.quiet_library_logs` filters every aiohttp logger
that could record request bytes: such a record keeps its level and logger name, never its
message text or exception, so no header value — a bearer secret included — reaches a log.

**INV-BRW-004**: Every `/v1` route requires `Authorization: Bearer <pairing secret>`, except `POST /v1/pair/claim` while the pairing window is open, and every `POST` requires `X-Casa-Browser-Protocol: 1`; both are checked before the route acts.

Every refusal is `{"error": "<code>"}` with one status per code (a refusal of a `HEAD`
request has the status and, as HTTP requires, no body):

| Code | Status | Code | Status |
|---|---|---|---|
| `unauthorized` | 401 | `conflict` | 409 |
| `protocol_mismatch` | 400 | `stale_handle` | 409 |
| `unsupported` | 400 | `interrupted` | 409 |
| `blocked` | 403 | `file_too_large` | 413 |
| `not_found` | 404 | `internal` | 500 |
| `judgement_stale`, `digest_mismatch` | 409 | `not_ready` | 503 |

`not_found` covers an unknown id, an id of another run, an unknown path and a method a path
does not serve (there is no implicit `HEAD`); every one of them is answered after the guard. `conflict` covers a changed
request under a used `request_id`.

Routes (`{run}` scoped routes answer only for that run's own ids):

| Route | Answers |
|---|---|
| `GET /v1/hello` | `{protocol_version, app_version, configured, needs_locale, language_source}` |
| `POST /v1/pair/claim` | `{secret, protocol_version}` to the first claim in the window; `unauthorized` otherwise |
| `GET /v1/sites`, `POST /v1/control/sites` | `{sites: [{site_id, display_name, status}]}`; `{site_id}` |
| `POST /v1/runs`, `GET /v1/runs?state=active`, `GET /v1/runs/{run}` | the run (201 new, 200 repeat); `{runs: [ActiveRun]}`; the run with `stopped` (its request id is in `cancelled_requests`) |
| `GET /v1/runs/{run}/events?after=N` | `{events: [...]}` in sequence order |
| `POST /v1/runs/{run}/{observe,action,search,download,archive}` | `Observation` (with `{"sign_in": true}`, a page with sign-in fields answers a `sign_in` judgement first: [`casa-browser-signin.md`](casa-browser-signin.md)); action result; `Observation`; file record; file record |
| `POST /v1/runs/{run}/fetch` | a step: a public address read with no browser ([`casa-browser-files.md`](casa-browser-files.md)) |
| `POST /v1/runs/{run}/{look,read}`, `GET /v1/control/runs/{run}/judgements/{judgement}/picture` | `{picture}`; `{text, next_offset, complete, masked}`; a PNG ([`casa-browser-contract.md`](casa-browser-contract.md)) |
| `POST /v1/runs/{run}/live` (`login`, `assist`), `POST /v1/control/runs/{run}/live-complete` | live-view envelope; the run |
| `POST /v1/runs/{run}/{finish,cancel}`, `POST /v1/control/sites/{site}/forget` | the run; `{site_id, status}` |
| `POST /v1/requests/cancel` | `{requests: [{request_id, run_id, stopped, state}]}` (below) |
| `GET /v1/records?requester_id=&since=`, `GET /v1/notices?after=N` | `{records}`; `{notices: [{seq, …}]}` |
| `GET /v1/files/{file}`, `POST /v1/control/files/{file}/delivery` | bytes; delivery acknowledgement |
| `GET /v1/control/runs/{run}/screenshots/{screenshot}`, `GET /v1/control/runs/{run}/sources` | PNG; `{sources: [{source_id, url, title_masked}]}` |

Registered origins are stored as `https://` origins in the spelling `identity.address` gives
(an explicit `:443` is dropped). `/v1/control/…` routes are trusted operator callbacks and delivery paths for Casa's plugin
code; they are never model tools. Observations, events and records never carry an address.

An account run names at least one site; an open-web run may name none, and a `start_url`.

**The request cancel** (`api.cancel_requests`, `controller.RunController.cancel_requests`) is
the requester's Stop of one request: every `request_id` it chose for it, 1–8 distinct caller
ids, in one call, whether or not runs of them exist yet. Every id is decided in one
synchronous step, with nothing awaited between: an id with no run is recorded (its create
comes back cancelled); a queued run is recorded and moved to `cancelled`; the driving run,
when its ending has not begun, is recorded, its in-flight command cut and its ending begun
(`_begin_end`, so a command reaching the lock later is refused); a run already terminal, or
one whose ending another command already began (a finish, an interruption), is not
recorded. Only then is the driving run torn down under the command lock. Each id's
`stopped` is whether it is recorded after the call, so a repeat answers the same, and
`GET /v1/runs/{run}` carries the same fact as `stopped` (`request_stopped`).

**Admission digest.** `POST /v1/runs` computes `protocol.spec_digest` over the members it
received, `admission` removed: SHA-256 of their RFC 8785 canonical JSON, `sha256:<hex>`.
The JSON rendering equals RFC 8785 because floats, non-ASCII keys, integers beyond 2^53−1
and non-JSON values are refused first. A digest that differs from `admitted_spec_digest` is
`blocked`; an unknown `origin_kind` is `unsupported`. `casa_browser/tests/vectors/spec_digest.json`
is the shared vector file a caller's implementation is checked against.

**INV-BRW-005**: A run is queued only when the SHA-256 of the RFC 8785 canonical JSON of the received members other than `admission` equals the admitted digest.

**Pairing.** `pairing.Pairing.arm` (an ingress operator action; no `/v1` route arms) starts
a new generation, drops the current secret, revokes every run and live grant through the controller's `revoke_authority`, and opens a window of exactly two
minutes. The first claim inside it receives a fresh 256-bit secret and closes it; a claim at
or after the deadline is refused; a restart closes an open window. The setup view shows the
caller's self-reported label HTML-escaped and marked as self-reported, beside the address the
claim came from. This is a first-caller race by design, not caller verification.

**INV-BRW-006**: Exactly one claim inside an armed two-minute window receives the secret, and arming revokes the previous secret and everything issued under it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/protocol.py`
- `casa_browser/rootfs/opt/casa_browser/pairing.py`
- `casa_browser/rootfs/opt/casa_browser/api.py`

**Tests**
- `casa_browser/tests/test_protocol.py`
- `casa_browser/tests/test_api_auth.py`
- `casa_browser/tests/test_pairing.py`
- `casa_browser/tests/test_digest.py`

**Related**
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-contract.md`](../reference/casa-browser-contract.md)
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
<!-- END SOURCEMAP -->
