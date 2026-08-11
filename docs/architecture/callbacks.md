---
last_reviewed: 2026-08-02
---

# Authorization callbacks

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The facility that lets a plugin receive an external provider's browser redirect — the return
leg of an OAuth-style authorization flow — at a public, unauthenticated `GET /callback/<name>`
URL, deposits the result into an on-disk spool, and nudges the plugin's agent to collect it.
It covers the public endpoint, the spool protocol, the per-flow attempt ledger, the consent
that opens a route, the reconciler that routes it, the worker that redelivers until receipt,
and the validated base URL every redirect URI is built from. It does
not cover webhook *triggers* (`architecture/triggers.md`), webhook authentication
(`architecture/http-surface.md`), or what the collecting turn then does with the code.

## Mental model

**Casa is the untrusted middle, not a party to the flow.** An external provider redirects a
browser to Casa carrying a bearer credential (an authorization code) in the query string; an
ephemeral plugin consumer, which minted the flow's `state`, later picks the result up. Casa
interprets nothing the consumer minted beyond a bounded, fail-closed envelope read — never on
the request path before a claim is won — and never keeps a credential past its own short TTL.
The endpoint produces **no turn**: no ingress-identity row, no clearance, no provenance, because
a browser redirect is not an authenticated principal.

**One wildcard route, allowlisted by an overlay.** As with triggers, there is a single
`GET /callback/{name}` route, not a route per callback; the name is looked up in a
plugin-callback overlay the reconciler swaps atomically (INV-CB-001). `/callback/done` is a
separate static route registered first, so the terminal redirect target can never be mistaken
for a callback name.

**One response always, and nothing query-derived logged.** Success and every refusal cause
return the *same* 303 redirect to the query-less `/callback/done` with the same headers, since a
differentiated status, header or target would be an enumeration oracle telling a prober which
names route and which states are live (INV-CB-005); there is deliberately no 429. The query
carries the credential, so handler logs carry a reason enum, a cid and the *effective* name,
with a fixed sentinel for the attacker-controlled unrouted name (INV-CB-006).

**Consent is narrower than a trigger's, and bound to the declaration, not the artifact.** What
the operator approves is only "an unauthenticated GET may deposit a query blob into this
plugin's spool" — no role turn, no memory access, so no target, clearance or auth policy to
disclose. The consent identity is `(plugin, effective name, declaration digest)`, and the digest
folds in only the declared name, so a routine upgrade that leaves `casa.callbacks` unchanged
keeps its ack and a re-authorization flow never opens a dark window — unlike an artifact-bound
trigger ack.

**The spool is a same-uid, mtime-clocked, publish-once protocol.** Every plugin process runs as
root in one container, so the spool is not an inter-plugin security boundary; its guarantees are
against *itself* racing, crashing, or a swapped symlink. A pending file's mtime is its mint time
and survives the claiming rename; each TTL runs off its own file's mtime. Publication is always
a `link(2)` of an already-complete inode whose `EEXIST` is the atomic arbiter — the claim has
exactly one winner however many processes race, and a replayed redirect can never rewrite a
result. A claim also pins the *identity* of the plugin's spool directory: each carries a random
`.dir-id` token minted at creation, and discard/publish refuse when the token no longer matches
the one captured at claim time, so a removal + reinstall mid-flow fails closed. The token
carries that rather than the `(st_dev, st_ino)` pair, which a filesystem may recycle straight
back to the recreated directory.

**One record per flow, facing both ways, and derived from the artifacts.** Beside `pending/`,
`results/` and `.claims/`, each plugin's spool carries `attempts/<state_hash>.json` — a small,
credential-free record casa writes for every minted state it observes. It is at once casa's
durable ledger and the consumer's read surface: a plugin process that died mid-flow lists
`attempts/` in its next life and learns what happened. Status walks
`awaiting_redirect → result_ready → done`, and a `done` record carries exactly one outcome —
`collected`, `expired`, `expired_unread`, `publish_failed` or `evicted`. It is never a second
source of truth: the artifacts are authoritative and every pass
re-derives the record from them, so a terminal record beside a live artifact of its hash gets
rewritten open, a `result_ready` record whose flow rewound to a live pending goes back to
`awaiting_redirect`, and an unreadable record is rebuilt from whatever survives — or retired as
an anomaly when nothing does.

**Three consumer verbs, each an atomic rename or link.** The spool lock serializes *casa*
only — every consumer is an independent filesystem actor — so each consumer-visible transition
is an operation whose loser is detectable: `mint` (publish-once `link(2)`), *collect* (rename
`results/<h>.json` → `.collect-<h>-<uuid>`, read after the rename), and *ack* (rename
`attempts/<h>.json` → `.ack-<h>`). An ENOENT where casa expected a file reads as the consumer's
move, and casa converges rather than mislabelling the flow. `mint` also carries the consumer's
own context — it writes `{"v": 2, "meta": <any JSON value>}`, an opaque blob casa stores, echoes
into the result and attempt records, and never interprets, so a successor process can recognize
its own flow across lives — together with the mint clock as `minted_ts` (the pending file's
mtime, preserved through the claim). `{"v": 1}` still mints, with a null `meta`. Bearer material
does not belong in `meta`, and whatever goes in inherits the attempt's retention.

Who owns each name in a plugin's spool (every dot-prefixed entry at the spool *root* is
reserved — excluded from plugin enumeration, sweep and orphan GC alike, which is what the
removal ledger relies on):

| Path | Created by | Mutated by | Deleted by |
|---|---|---|---|
| `pending/<h>.json` | consumer (`mint`) | nobody | casa (claim, sweep, ack-teardown) |
| `.claims/<h>` | casa | casa | casa |
| `results/<h>.json` | casa (publish-once) | nobody | consumer (rename) or casa (sweep, ack-teardown) |
| `results/.collect-<h>-<uuid>` | consumer (rename) | nobody | casa only — ack-teardown or aged/capped sweep |
| `attempts/<h>.json` | casa | casa | consumer (ack rename) or casa (age-out, cap, ack) |
| `attempts/.ack-<h>` | consumer (rename) | nobody | casa, after every artifact is a confirmed ENOENT |
| `.removals/<plugin>-<uuid>.json` | casa | casa | casa (pruned after the note, 30-day hard bound) |
| `ready.json`, `.index/*` | casa | casa | casa |

## Contracts & invariants

**INV-CB-001**: The callback route serves only names present in the consented overlay; a request for any other name performs no spool mutation.

Enforced by the wildcard handler, which looks the path component up in the reconciler's overlay
and, on a miss, returns the neutral redirect without touching the spool. The overlay is the sole
authority on what the endpoint serves — the spool's advisory `ready.json` marker cannot open a
route on its own.

What it does not cover: ingress routing only, not the credential's fate once deposited. The
marker is advisory here but load-bearing for setup dispatch, which holds until the pair on
disk equals the desired one (INV-PLUG-011).

**INV-CB-002**: A pending state is consumed at most once — the claim-by-rename is the consumption point — and a replayed redirect never rewrites or duplicates a result.

Enforced in the spool: a claim renames `pending/<hash>` into `.claims/` and a result is
`link(2)`-published, so a second arrival for the same state finds no pending to claim and a
result that already exists, and mutates neither — under concurrent threads and processes alike.

What it does not cover: it bounds duplication *inside the spool*. A consumer that collects a
result and then acts non-idempotently owns that idempotency itself.

**INV-CB-003**: A callback ack binds `(plugin, effective, declaration_digest)`; the ack store fails closed whole-store on any malformed or key-mismatched record; and plugin removal revokes its acks and unroutes.

Enforced by the ack store's load path — wrong schema, any malformed record, or any key that does
not equal its record's recomputed identity yields *no* acks at all, never a partial store — and
by the reconciler, which prunes an ack no installed declaration can still compute and swaps the
overlay so a removed plugin's route goes dark.

What it does not cover: the identity deliberately excludes the artifact id, so only an
operator-visible declaration change mints a new identity and forces re-consent.

**INV-CB-004**: Casa relays the query opaquely — the raw query string plus an ordered list of decoded key/value pairs — and interprets only `state`; no provider-specific parsing lives in core.

Enforced in the handler: `state` is extracted from the *raw* query against a fixed grammar (so a
percent-encoded or duplicated `state` is a rejection, not a smuggled decode), and everything else
is recorded verbatim alongside an ordered decoded view for the consumer. Duplicate keys and their
order are preserved; undecodable bytes decode with replacement rather than being rejected.

What it does not cover: the credential's *meaning* — casa neither validates nor understands the
provider's parameters beyond `state`.

**INV-CB-005**: For a syntactically-accepted GET that reaches the app, success and every refusal cause yield the same status, headers and redirect target, at any traffic level — there is no throttle response.

Enforced by routing every outcome through one neutral-redirect builder and by wrapping the whole
request path so no fault escapes as a differentiated 500; the per-key sampler damps only internal
log emission, never the response, and a drained sampler bucket still answers identically.

What it does not cover, by construction: rejections that never reach the app — a proxy-level
refusal or rate limit, request timing, a non-GET method (the framework's own 405) and an
over-length request line (its 400) are outside the uniformity guarantee.

**INV-CB-006**: The callback query string never reaches the app access logger, the app handler and exception logs, or the in-container nginx access logs; an unrouted name logs a fixed sentinel.

Enforced on three surfaces: the access logger suppresses the query for the `/callback/` prefix;
the handler interpolates no request data into its log lines or static pages and logs the
sentinel for an unrouted name; and an installed `logging.Filter` on the `aiohttp.server` logger
redacts the whole request target — path and query, message *and* exception traceback — from an
over-length request line that raises below the handler (the entire target, not just the query,
so no inner quote can leave a fragment behind). The in-container nginx access log applies the
same suppression by a `map` rule.

What it does not cover — documented, tested residuals: the in-container nginx *error* path on an
upstream failure, and the outer reverse proxy's own logs, which are operator-configured. The
`/callback/` access line still records the path (only its query is dropped).

**INV-CB-007**: Every minted state a casa pass observes ends in exactly one of two ways — a consumer ack, or one terminal outcome written durably before the flow's last credential-bearing artifact is deleted and retained until that ack or the attempt retention bound.

Enforced by making every flow-retiring deletion write-ahead — a pending or result aged out or
evicted, an aged `.collect-*` hold, a stale claim dropped by recovery, a hash-named anomaly.
Each writes its outcome to the attempt file first, with a strict fsync of file and directory,
and a fsync that does not confirm *skips the deletion* that pass, so a power loss can never
take credential and record together. Publish records synchronously rather than by a deferred
kick: a staging or link failure writes `done/publish_failed` before `publish_result` returns,
and only that proven-durable record authorizes the handler to discard the claim — an unrecorded
failure leaves the claim for recovery. Moving a live flow between artifact classes (pending to
claim, claim to result) is a custody transfer needing no record. An ack supersedes the record
entirely: consuming an `attempts/.ack-<h>` token retires every artifact of the hash and deletes
the token strictly last, once each is a confirmed ENOENT.

What it does not cover: plugin removal is the documented exception — per-flow files die with the
plugin, and the abort's record is the strict-durable `.removals` entry written before the purge.
Labels are coarse where casa cannot attribute more precisely: `expired` for anything recovery
cannot distinguish, `expired_unread, claimed: true` for a consumer that renamed a result but
died before its own commit point. The union a removal counts is check-then-act against a
same-uid FD holder POSIX gives no way to exclude — such a mint lands in an already-unrouted
plugin, so only its abort notice is lost. A disk fault
taking the outcome write itself leaves no record at all.

**INV-CB-008**: The delivery nudge re-fires on a durable schedule until receipt — the attempt leaving `result_ready` — or until the budget of six accepted dispatches is spent, whichever comes first, and a spent budget raises exactly one operator note.

Enforced by a worker that selects from the attempt ledger rather than from a dispatch mark, so
"the bus accepted a turn" is not terminal. Bus acceptance advances `next_nudge_ts` through the
result-phase offsets (0, +60 s, +3 min, +8 min from the
result inode's mtime, its durable publish time) and afterwards the outcome-phase offsets
(+30 min, +2 h from `ended_ts`), spending one budget unit each time; it never ends delivery by
itself. A pass that exhausts its in-pass retries without an accept spends no budget and defers
on an escalating capped delay, so an unavailable bus yields a bounded cadence, not a spin.
Schedule and `noted` flag live in the attempt file, so a restart resumes rather than re-spams,
and the worker waits on a timeout derived from the nearest due entry — the request-path `kick`
is an optimization, not the source of work. A `collected` attempt never nudges.

What it does not cover: acceptance is still the per-dispatch mark, so a crash between it and the
ledger write costs one duplicate nudge — collection is idempotent for the consumer by contract.
The budget bounds delivery attempts, not success: an attempt whose consumer never runs spends
its six dispatches, draws the note, then rests as a terminal unacked record until the retention
bound. Where no assigned role resolves, the nudge stays due and the operator is noted once per
streak.

**INV-CB-009**: The consumer's `meta` is size-capped, stored and echoed value-preserving, never interpreted, and never reaches a log surface.

Enforced by an envelope parser that is total and fail-closed: a read bounded at 4 KiB plus one
byte, a UTF-8 JSON object with no non-finite number anywhere in it, a version in `{1, 2}`,
unknown keys dropped and never copied. Any defect yields a null `meta` rather than a refusal —
by the time casa looks the state is already consumed, so refusing buys nothing. The value is
parsed once and re-serialized in the one canonical byte form into the result and attempt records
(value-preserving, not byte-verbatim); nothing downstream walks it, so a deeply nested blob
cannot become a recursion fault. Parse diagnostics log an error class only, with no `exc_info`
that could render the value, and worker, sweep and removal diagnostics carry a plugin name, a
reason enum and an errno — never a hash, state, query, `meta` or raw `OSError`, whose text names
the entry it failed on. The one hash casa composes for a human is the flow *handle* in the nudge
text, which is not a log line — INV-CB-006's discipline extended to the attempt surface.

What it does not cover: casa cannot inspect an opaque value, so "no bearer material in `meta`"
is a consumer obligation, not enforced.

## Failure behavior

**An unrouted name, or a missing, malformed, expired, replayed or never-minted state.** All
collapse to the same neutral redirect with no spool mutation; an unrouted name logs the fixed
sentinel rather than the attacker-controlled component, and the rest log `no_pending`, the spool
refusing them identically (`expired` is the sweep's vocabulary, not the handler's).

**A result write fails.** `done/publish_failed` is recorded before `publish_result` returns, and
only that proven-durable record authorizes discarding the claim, which keeps the state consumed
(single-use); a failure that could not be recorded leaves the claim, and recovery restores the
flow to `pending/`. No partial result is published, and the response is still the neutral
redirect (INV-CB-002, INV-CB-005, INV-CB-007).

**A published result is never collected.** It is deleted at `RESULT_TTL_S` — 900 s, past the
life of an authorization code at its provider — but not silently: the sweep records
`done/expired_unread` on the attempt first, and that record outlives the credential until the
consumer acks it or the seven-day retention bound — so a plugin that only runs tomorrow still
learns the flow's fate.

**An internal fault on the request path.** Absorbed by the outer guard into the same neutral
redirect (INV-CB-005).

**The ack store is missing, unreadable, or malformed.** Treated as zero acks; callbacks stay
unrouted, and the next successful `record` rewrites a valid store. It never raises into the
reconciler (INV-CB-003).

**`PUBLIC_URL` is unset or not a clean `https://` origin.** No redirect URI can be built; every
otherwise-routable plugin surfaces `callback_base_url_invalid`, and no readiness marker or index
entry is written. A bare IP, a path, userinfo or an embedded control character is rejected the
same as unset.

**A reconcile compute fails.** The overlay fails closed to empty and the pruning of stale acks is
skipped — a resolution hiccup must never vaporize consent.

**A delivery nudge (`kick`) is lost to a crash.** The kick is only a hint: every pass — boot,
periodic recovery, the worker's timed wake — re-derives the ledger from the artifacts and
redelivers what is due, so delivery converges on the durable schedule, not on a request-path
signal. A repeated nudge is idempotent: the consumer's collection against an emptied directory
finds nothing (INV-CB-008).

**A plugin is removed with authorizations in flight.** Removal is abort-with-notice: casa counts
every unsettled hash — each attempt record, open or terminal, plus every live pending, claim,
result and consumer-held collect entry, less every hash a receipt token covers, an ack settling
the flow and not just its ledger entry — and on a non-zero count writes a strict-durable
`.removals` record before purging the directory. No purge proceeds on an unproven answer — a
record that will not go durable, or a listing or metadata read that faults, defers it rather than
reading as empty — at removal and at the orphan GC alike, which writes the same record before
purging a quiescent directory holding unacked attempts. The worker turns each
un-noted record into one operator note, notifying first and marking only a confirmed send, so a
crash there costs a duplicate rather than silence (INV-CB-007).

## Extension points

**A new plugin callback** is a `casa.callbacks` entry naming a redirect endpoint — a peer of
`casa.triggers` but carrying only a name (no target, clearance or auth block). It routes only
after intrinsic validation passes, the plugin holds at least one role, and a persisted operator
ack for the exact consent identity exists; the declaration has hard rails a plugin author cannot
infer from the routing model — at most four callbacks per plugin and an effective name
(`plg-<plugin>--<declared>`) capped at 128 characters. Reconciliation runs at the trigger
reconciler's seams: boot, plugin lifecycle, the trigger-affecting reload scopes, consent
approve, revoke.

**A bundled or sourced specialist dependency** *may* declare `casa.callbacks` — unlike
`casa.triggers`, which such a dependency may not — because a callback grants no turn or memory
access. Its owned registry entry routes under the *scoped* name (`slug.manifest_name`), so an
inspect-time gate refuses a callback whose scoped effective name would overflow the 128-char cap
(`callback_name_too_long`) before the entry can reach the registry.

**A single-callback off-switch** is the `callback_ack_revoke` tool: it drops every ack for one
`(plugin, effective)` callback across any declaration digest and reconciles, darkening that
route until the operator re-consents.

**An expired consent DM is recovered on demand by `consent_reprompt`** (see the tool
interface); a consent relayed through any ask surface commits nothing.

**Registering a redirect URI with a provider** uses `redirect_uri`, joining the validated base
with `callback/<effective>` — the exact string the provider must be given, matched byte-for-byte
on the return leg, and the one place a malformed value could reach a third party.

**Writing a consumer** means importing the protocol rather than re-implementing it:
`callback_spool` ships `mint`, `collect` and `ack` as the executable half of the contract.
Discover the spool through its `.index` entry, `mint(state, meta)` to start a flow, and on any
later life list `attempts/` (ignoring `.ack-*`). A `result_ready` record is collected by rename
and read *after* it; an ENOENT there is retryable and never ackable, since the attempt becomes
visible a moment before the result link lands. Persist the exchange in the consumer's own store
first, then ack — and **never unlink the held `.collect-*` file**, the flow's crash journal
until ack-teardown removes it with every other artifact of the hash. A `done` record is acted on
and acked the same way; acking an `awaiting_redirect` attempt is the abort verb.

**Relying on a nudge alone for durability** is the wrong model: the guarantee lives in the
attempt ledger every pass re-derives, not in the `kick`. A new result-delivery path
must leave an attempt file, or it can silently drop.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/callback_http.py::make_callback_handler`
- `casa/rootfs/opt/casa/callback_http.py::make_done_handler`
- `casa/rootfs/opt/casa/callback_http.py::install_callback_log_redaction`
- `casa/rootfs/opt/casa/callback_http.py::OutcomeSampler`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool.claim`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool.publish_result`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool.attempts_pass`
- `casa/rootfs/opt/casa/callback_spool.py::CallbackSpool.remove_plugin`
- `casa/rootfs/opt/casa/callback_spool.py::mint`
- `casa/rootfs/opt/casa/callback_spool.py::collect`
- `casa/rootfs/opt/casa/callback_spool.py::ack`
- `casa/rootfs/opt/casa/callback_attempts.py::parse_envelope`
- `casa/rootfs/opt/casa/callback_attempts.py::terminalize`
- `casa/rootfs/opt/casa/callback_attempts.py::next_nudge_after_accept`
- `casa/rootfs/opt/casa/callback_acks.py::CallbackAckStore`
- `casa/rootfs/opt/casa/callback_consent.py::render_callback_consent_message`
- `casa/rootfs/opt/casa/callback_reconcile.py::reconcile_plugin_callbacks`
- `casa/rootfs/opt/casa/callback_reconcile.py::compute_desired`
- `casa/rootfs/opt/casa/callback_episodes.py::kick`
- `casa/rootfs/opt/casa/callback_episodes.py::recovery`
- `casa/rootfs/opt/casa/callback_urls.py::validated_base`
- `casa/rootfs/opt/casa/callback_urls.py::redirect_uri`
- `casa/rootfs/opt/casa/plugin_callbacks.py::parse_and_validate`
- `casa/rootfs/opt/casa/plugin_callbacks.py::ack_identity`
- `casa/rootfs/opt/casa/plugin_callbacks.py::declaration_digest`

**Tests**
- `tests/test_callback_http.py`
- `tests/test_callback_spool.py`
- `tests/test_callback_attempts.py`
- `tests/test_callback_episodes.py`
- `tests/test_callback_acks.py`
- `tests/test_callback_reconcile.py`
- `tests/test_callback_consent.py`
- `tests/test_plugin_callbacks.py`
- `tests/test_callback_urls.py`

**Related**
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
<!-- END SOURCEMAP -->
