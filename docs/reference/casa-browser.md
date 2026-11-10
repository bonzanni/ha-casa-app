---
last_reviewed: 2026-10-22
---

# Casa Browser — options, state, protocol v1 and request policy

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The optional second app in this repository, `casa_browser/` (slug `casa_browser`, its own
version line and `casa_browser-vX.Y.Z` tags; the release pipeline is
[`../contributing/release-pipeline.md`](../contributing/release-pipeline.md)). This document
covers what the app's code holds today: its manifest and options, root bootstrap, durable
state and retention, the versioned `/v1` HTTP contract with its pairing, and the pure
address and request-policy functions. The kernel egress floor and the enforcing proxy that
apply that policy to real traffic are [`casa-browser-runtime.md`](casa-browser-runtime.md).
Chrome, its launcher and the display are [`casa-browser-browser.md`](casa-browser-browser.md).
The ingress pages and the live view are [`casa-browser-ingress.md`](casa-browser-ingress.md);
the `/v1` routes are exercised against `casa_browser/tests/fake_controller.py` as well as the
real controller.

The app's code is the package `casa_browser` under `casa_browser/rootfs/opt/`; the outer
`casa_browser/` folder is not an import package. Its tests run in their own Python 3.13
environment (`venv_browser/`, `make test-browser`), separate from Casa's.

## Mental model

**Three trust zones, one direction of trust.** Casa's trusted plugin code calls the app
with a shared secret; root bootstrap is the only code that reads the app options; and the
policy functions decide, per request, whether a browser request may leave. A model never
holds the secret, an option value or a raw address: those stay in controller-private state, and every wire type that carries one excludes it from `repr`
and from its `to_wire` projection (`Source.url` is the one exception, on a trusted route).

**Sessions, not approvals.** Nothing is approved per request: an account session's own
pages are free, its top-level documents stay on the site, and every action the agent takes on
a signed-in site is checked as a whole ([`casa-browser-actions.md`](casa-browser-actions.md)).

## Options and root bootstrap

| Option | Default | Consumer |
|---|---|---|
| `onepassword_service_account_token` | empty | broker config only |
| `onepassword_vault` | `Casa Browser` | broker config only |
| `guest_sign_in_address` | empty | broker config; the proxy, as the account host |
| `guest_email`, `guest_password`, `guest_secret_key` | empty | broker config only |
| `language` (`str?`) | unset: follows Home Assistant | effective locale |
| `timezone` (`str?`) | unset: follows Supervisor's `TZ` | effective locale |
| `retention_days` (`int(1,3650)`) | 30 | retention sweep |
| `log_level` | `info` | services |

The manifest asks for `homeassistant_api`, `video`, `ingress` (port 8099) and
`privileged: [NET_ADMIN]` only; no Supervisor API, host network, port mapping or shared
folder. `casa_browser/apparmor.txt` is the app's own AppArmor profile, validated on a Home
Assistant OS test host (complain, then enforce): Docker's default rules with fewer
capabilities, the Chromium sandbox's `sys_admin` and `sys_chroot` only in a child profile for
Chrome, `sys_ptrace` denied outright, and no raw or packet sockets (`test_apparmor_profile`). `backup_exclude: ["profiles/*", "downloads/*", "proxy/*"]` keeps saved browser
profiles, the browser's downloads directory and the proxy's private binding key out of HA
backups (Supervisor matches each pattern against the end of the file's full path, glob-style, and
does not descend into an excluded directory).

The s6 oneshot `init-browser-config` runs `browser-config.sh`, which runs
`options.bootstrap` as root before any service: it makes `/data/options.json` `0600` (a
symlink there is neither read nor followed: the options count as invalid);
creates `/data/state`, `/data/artifacts` (controller), `/data/profiles` (browser),
`/run/casa-browser/broker` (broker) and `/run/casa-browser/proxy-private` (proxy), each
`0700`, and `/data/downloads` (browser user, controller group, `2770`), emptied at every start
([`casa-browser-files.md`](casa-browser-files.md)); writes the broker's credentials to `broker.json`, `0600`, owned by the broker user —
empty credentials when the options did not validate; writes the account host validated from
`guest_sign_in_address` (`options.account_host`: one `<team>.1password.<eu|com|ca>` host, never
the generic `my.` address) to the proxy's `hosts.json`, `0600`; writes the secret-free options and the effective locale to controller state; and
removes `SUPERVISOR_TOKEN` and `HASSIO_TOKEN` from `/run/s6/container_environment`, the only
place `with-contenv` reads a service's environment from.

**INV-BRW-001**: Root bootstrap removes the Supervisor token from the s6 container environment on every start, also when bootstrap fails, and writes it to no file or log.

The **effective locale** is the `language` option if set, else the `language` member of
Home Assistant's `GET /core/api/config` read through the Core API proxy; the time zone is
the `timezone` option, else `TZ`. Either one missing or invalid gives `needs_locale`, which
`GET /v1/hello` reports and which holds new runs; there is no default language. A changed
HA language takes effect at the next app start. An invalid option is named (never its value)
in the log and in the public options record.

Service users have fixed ids: `casa-browser` 1100, `casa-proxy` 1101, `casa-broker` 1102,
`casa-controller` 1103. `options.child_environment` passes only `PATH`, `LANG`, `LC_ALL`
and `TZ` to a child process.

## Durable state and retention

`store.Store` is one SQLite database (WAL, foreign keys, ordered migrations): sites, runs,
per-run step events, run records, file and screenshot metadata with delivery attempts
([`casa-browser-files.md`](casa-browser-files.md)), a run's questions and stored responses
([`casa-browser-approvals.md`](casa-browser-approvals.md)), a run's public-session sources ([`casa-browser-runs.md`](casa-browser-runs.md)), the
proxy binding key each site's provider binding was learned under (by its id, never the key;
[`casa-browser-ingress.md`](casa-browser-ingress.md)), durable notices and the pairing state
(the secret's SHA-256 only).

- A run is idempotent on `request_id`: the same members return the existing run, different
  members are `conflict`.
- `cancelled_requests` (migration 10) holds the request ids the operator's Stop stopped: a
  run later created for such an id is created `cancelled` and gets only its `run_cancelled`
  event, and `lease_next_run` never leases a queued run whose id is recorded, so no browser
  starts for it.
- `lease_next_run` moves the oldest `queued` run to `starting` only while no run is between
  `starting` and `finishing`; `transition` is a compare-and-set and never moves a terminal
  run.
- `interrupt_nonterminal` (app start) interrupts every non-terminal run, queued ones
  included, with one durable notice each (deduplicated, so a second start adds none).

**INV-BRW-002**: At most one run is between `starting` and `finishing`; the FIFO lease is taken in one immediate transaction.

`retention.sweep` deletes runs that ended at least `retention_days` ago, with their events,
records, files, screenshots and older notices: it queues the private paths and deletes the
rows in one transaction, then unlinks each queued path and dequeues it; a path that could not
be unlinked stays queued for the next sweep. A symlinked artifacts root is refused outright.
A run's questions, stored responses and its files' delivery attempts go with it, and a
cancelled request id goes once it was recorded before the cutoff. Sites, pairing
and profiles are never swept. Migration 9 drops the tables of the former approval model
(`approvals`, `bundles`, `hints`) and the events' `bundle_id` column; every other row stays.

**INV-BRW-003**: The retention sweep unlinks only paths whose parent directory resolves inside the artifacts directory, and removes a symlink itself, never its target.

## Credentials

The broker's registry, vault facade, login flow, private RPC sockets, service environment
and durable credential notices are described in
[`casa-browser-credentials.md`](casa-browser-credentials.md).

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

## Addresses

`identity.address(url)` is the floor's URL check: it returns the canonical origin (scheme and
host lowercased, default port dropped, `ws`/`wss` as `http`/`https`) and the raw path, query,
fragment and whether each delimiter was present, or raises `IdentityError`. URLs with userinfo,
whitespace or control characters, backslashes, non-ASCII or percent-encoded hosts, an empty or
zero port, or another scheme are refused. `identity.SEMANTIC_HEADERS` names the headers that
change what a server does with a request (`content-type`, `content-encoding`, `next-action`,
the method-override headers, `soapaction`); the infrastructure shapes and the proxy read it.

## Request policy

`policy.decide(request, policy, extra_hosts, tagger=, hop=)` is pure and reads no approval.
In order: no policy refuses; the public-address floor (scheme, no userinfo, global unicast IP
literals and resolved addresses, no `localhost`, single-label or local-suffix hosts, and no
host whose last label is a number unless it is a canonical dotted quad — a browser reads such
hosts as IPv4); an address that does not parse refuses (`unidentifiable`); a session that does
not fit the run refuses (`session_mismatch`); the account session's fixed infrastructure rules
(`infra.py`: an auth vendor's session refresh on the active site's login origins, Cloudflare's
challenge path on task origins, hCaptcha `checksiteconfig`, each bounded in path and query)
admit; Chrome's own background sends refuse (`browser_background`). Then:

- **A download hop** (`hop=True`, the downloader's listener) is admitted (`download_hop`).
- **The public session**, in any run, admits any origin (`public_session`); it is the
  ephemeral profile, with no account cookie and no 1Password unlock.
- **The account session.** A call the browser or its extension makes on its own off the run's
  sites refuses quietly (`browser_background`). A live-view `manual` lease admits task origins.
  A `login` lease admits the active site's login origins and the exact recorded provider
  binding; under it, a query with a decoded `client_id` or `redirect_uri` name in any case
  requires an exact binding (malformed names, encoded or uppercase key spellings and duplicate
  keys cannot match; the binding compares keyed tags of the raw path and values, never matches
  without a tagger, and is stored as the provider origin and three 64-hex tags only). Then the
  one rule of the session's reach: a top-level
  document — `resource_type` `document`, or a request whose class the browser did not state
  (no `Sec-Fetch-Dest`; plain http carries none, a navigation included) — must be on the
  active site's origins or login origins (`site_document`), else `site_lock`. Every other
  request of its pages — scripts, data, subframes, WebSockets, preflights — is admitted
  (`site_page`). The agent's own navigations off the site move to the public session first
  ([`casa-browser-runs.md`](casa-browser-runs.md)).

Every refusal is a quiet `Decision` with a fixed reason and event kind; nothing in `decide`
asks a question.

**One general check before every fixed exception.** The infrastructure rules and the provider
binding each declare a shape (`infra.Shape`: methods, body bound, the kept semantic headers
allowed — at most `content-type`, only where a body is declared — resource classes) and
`infra.within_shape` refuses the exception to a request carrying anything beyond it, or a
`.`/`..` path segment; such a request is decided as an ordinary one.

**INV-BRW-040**: In an account session the policy admits a top-level document — or a request whose class the browser did not state — only on the active site's origins and login origins, on a run site's origins or login origins during a live view's manual lease, or as the exact recorded provider binding during the run's login lease.

**INV-BRW-041**: In every session, and for every download hop, a request to an address the floor refuses is refused, Chrome's own background sends to its background hosts are refused, and a fixed exception — an infrastructure rule or the provider binding — admits a request only within its declared shape.

The fixed 1Password extension and broker SDK host lists in `infra.py` are empty: the
account's own host, from the options, and its region's service hosts join the extension list
at runtime through `decide`'s `extra_hosts`, and `infra.py` also holds the rules for Chrome's
own extension updates (see [`casa-browser-runtime.md`](casa-browser-runtime.md)).

## Known limits

**Request buffering in the proxy.** Responses are never buffered: every response streams to the
browser as it arrives. What the proxy keeps after deciding is bounded too: the event log it
holds for the controller (each request's origin and decision) is limited to 10,000
events and 64 MiB, the oldest dropped first and counted as dropped; an open WebSocket keeps only
the message being decided; the table of checked addresses keeps at most 4,096 hosts; and the
bodies of finished requests, which mitmproxy's flows hold in reference cycles, are freed by a
full garbage collection every two seconds rather than whenever Python's allocation counts call
for one. So ordinary browsing — image-heavy pages, form posts, uploads, or a page sending many
requests — stays well below the proxy's memory limit. The enforcing proxy decides a request only
once its whole body has arrived, so it bounds bodies before reading them: every request a
browser can build is bounded — a body over 1 MiB, a chunked HTTP/1.1 body, or an HTTP/2 request
with no declared length (except GET, HEAD and a CORS preflight, which a browser cannot give a
body) is refused at its headers and its stream closed unread. Two buffers inside mitmproxy
itself have no bound of their own: the data frames an HTTP/2 client sends after a GET, HEAD or
preflight that declared none, and the bytes of an HTTP/1 request head that never ends. A browser
never sends either; only a client writing its own frames on the browser's listener — an
already-compromised browser process — can fill them. Two more can be filled by a page itself,
though no ordinary page comes near the limit: a WebSocket message is held whole until its last
fragment arrives (mitmproxy has no message-size limit and no hook before a message completes),
and every upload in flight is held about three times over (some 300 parallel 1 MB uploads
approach the limit, measured). So a hostile page can force the proxy to restart whenever it
likes; the restart fails closed, as below (accepted, operator ruling 2026-10-21). If any of
these is fed to the limit, the proxy reaches its 1 GiB data limit (`svc-browser-proxy`'s
`prlimit --data`: private writable mappings, used or not, with two malloc arenas and a fixed
mmap threshold so freed request bodies go back to the system; the inaccessible reservations each
thread's malloc arena makes fall outside it, whereas an address-space cap would count them and
break ordinary concurrent browsing) and exits on its first `MemoryError`, including one
mitmproxy would otherwise catch and log per connection (one raised inside a policy hook refuses
that request instead); s6 restarts it holding no policy. Nothing is forwarded undecided: every
open browser connection drops and every request is refused until the controller installs a
policy again. The proxy does not patch mitmproxy's private code to bound them (accepted
residual, operator ruling 2026-10-20): the outcome is a restart that fails closed, never a
request forwarded undecided.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/config.yaml::options`
- `casa_browser/config.yaml::schema`
- `casa_browser/Dockerfile`
- `casa_browser/rootfs/etc/s6-overlay/scripts/browser-config.sh`
- `casa_browser/rootfs/opt/casa_browser/options.py`
- `casa_browser/rootfs/opt/casa_browser/store.py`
- `casa_browser/rootfs/opt/casa_browser/retention.py`
- `casa_browser/rootfs/opt/casa_browser/protocol.py`
- `casa_browser/rootfs/opt/casa_browser/pairing.py`
- `casa_browser/rootfs/opt/casa_browser/api.py`
- `casa_browser/rootfs/opt/casa_browser/identity.py`
- `casa_browser/rootfs/opt/casa_browser/policy.py`
- `casa_browser/rootfs/opt/casa_browser/infra.py`

**Tests**
- `casa_browser/tests/test_scaffold.py`
- `casa_browser/tests/test_generic_terms.py`
- `casa_browser/tests/test_options.py`
- `casa_browser/tests/test_store.py`
- `casa_browser/tests/test_retention.py`
- `casa_browser/tests/test_protocol.py`
- `casa_browser/tests/test_api_auth.py`
- `casa_browser/tests/test_pairing.py`
- `casa_browser/tests/test_digest.py`
- `casa_browser/tests/test_policy.py`
- `casa_browser/tests/test_task4_resolutions.py`

**Related**
- [`contributing/release-pipeline.md`](../contributing/release-pipeline.md)
- [`reference/operator-options.md`](../reference/operator-options.md)
<!-- END SOURCEMAP -->
