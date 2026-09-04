---
last_reviewed: 2026-08-02
---

# Authorization callbacks

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The ingress half of the facility that lets a plugin receive an external provider's
browser redirect — the return leg of an OAuth-style authorization flow — at a public,
unauthenticated `GET /callback/<name>` URL. It covers the public endpoint, the consent
that opens a route, the reconciler that routes it, and the validated base URL every
redirect URI is built from. The on-disk spool the result is deposited into, the per-flow
attempt ledger, and the worker that nudges the plugin's agent until receipt are
[`architecture/callback-delivery.md`](callback-delivery.md). It does not cover webhook
*triggers* (`architecture/triggers.md`), webhook authentication
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

## Contracts & invariants

**INV-CB-001**: The callback route serves only names present in the consented overlay; a request for any other name performs no spool mutation.

Enforced by the wildcard handler, which looks the path component up in the reconciler's overlay
and, on a miss, returns the neutral redirect without touching the spool. The overlay is the sole
authority on what the endpoint serves — the spool's advisory `ready.json` marker cannot open a
route on its own.

What it does not cover: ingress routing only, not the credential's fate once deposited. The
marker is advisory here but load-bearing for setup dispatch, which holds until the pair on
disk equals the desired one (INV-PLUG-011) and, separately, reads this overlay itself: it
defers while the overlay carries the unavailable marker at the moment of dispatch, or while
any publication has landed since its route check began (INV-PLUG-016).

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

**INV-CB-010**: A callback reconcile whose caller is cancelled holds the reconcile lock until the marker and ack writes it has already handed to a worker thread have settled, however many cancellations arrive; and a pass that had already published its overlay delivers one setup-worker kick after those writes settle and before the cancellation propagates.

The reconcile takes three write hops in worker threads under its lock — retire the pairs about to
stop being published, publish the routed set's pairs after the swap, prune stale acks — and a
thread handed work cannot be stopped. Each hop is therefore awaited through a shield and, on
cancellation, drained until that same future settles, through every further cancellation rather
than only the first. Released at the first one, the lock would let a successor compute against
files the orphan has not finished writing.

The kick is the terminal act of the two hops that follow the swap, and it fires only once the
future has settled — never from a `finally`. The [setup-dispatch gate](plugin-setup.md) reads the
marker pair under the spool's own lock, so a worker woken between the two writes of a pair reads a
half-published one, which the writer then deletes and records as a `callback_spool_error`: the
same held obligation, reached the other way round. The trigger half's fence
([`plugin-triggers.md`](plugin-triggers.md), INV-TRIG-017) deliberately kicks nothing on
cancellation, and the difference is not an inconsistency: that pass leaves its unavailable marker
standing for the scheduled recovery to collect, while this one has already swapped a live map, so
no marker stands, the recovery predicate reads nothing to recover, and the wake is owed here or
nowhere. The wait is bounded by the thread's own work and by nothing external.

Cancelled before the swap, the pass still drains — the deletes must land before another pass may
compute — but publishes nothing and kicks nothing, because nothing of its own has become both
durable and live. That silence is not a lost wake: the pre-swap half retires only orphans, which
the gate's mirror never examines, and pairs that already differ from the desired one, which is
the very condition that mirror records as a gap. Every pair it can delete was therefore already
one the gate was holding on, and the cancellation leaves that hold exactly as it found it. Nothing downstream may depend on a kick arriving: it is an `Event.set()`, and
the obligations it wakes keep their own reads (INV-PLUG-011, INV-PLUG-016).

## Failure behavior

**An unrouted name, or a missing, malformed, expired, replayed or never-minted state.** All
collapse to the same neutral redirect with no spool mutation; an unrouted name logs the fixed
sentinel rather than the attacker-controlled component, and the rest log `no_pending`, the spool
refusing them identically (`expired` is the sweep's vocabulary, not the handler's).

**An internal fault on the request path.** Absorbed by the outer guard into the same neutral
redirect (INV-CB-005).

**The ack store is missing, unreadable, or malformed.** Treated as zero acks; callbacks stay
unrouted, and the next successful `record` rewrites a valid store. It never raises into the
reconciler (INV-CB-003).

**`PUBLIC_URL` is unset or not a clean `https://` origin.** No redirect URI can be built; every
otherwise-routable plugin surfaces `callback_base_url_invalid`, and no readiness marker or index
entry is written. A bare IP, a path, userinfo or an embedded control character is rejected the
same as unset.

**A reconcile compute fails.** The overlay fails closed and the pruning of stale acks is
skipped — a resolution hiccup must never vaporize consent. What it fails closed TO is the
typed unavailable marker, not an empty map, for the reason
[`plugin-triggers.md`](plugin-triggers.md) gives at length: an empty map is the authoritative
claim that nothing should route, and a pass that computed nothing has not earned it. A
registry that could not be read reaches the same marker, because that case returns normally
rather than raising. Ingress is closed identically either way — `/callback/{name}` 404s —
and both halves of this pair publish the same marker from the same reconcile, since a trigger
revoke can shift callback assignment too. Reopening is paired the same way: while either
marker stands, the scheduled recovery pass [`plugin-triggers.md`](plugin-triggers.md)
describes recomputes both halves together, so a transient failure does not leave this half
shut until an operator action.

**A pass is cancelled.** It is released only after the writes it had already handed to a worker
thread have landed, however many cancellations arrive; a pass cancelled after its overlay swap
then wakes the setup worker once, and one cancelled before it publishes and wakes nothing
(INV-CB-010). Either way no reader sees a half-published pair, and no obligation is left held on a
marker pair this pass completed with nobody coming.

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

**An expired consent DM is recovered on demand by `consent_reprompt`** (see
[`plugin-mutation-tools.md`](plugin-mutation-tools.md)); a consent relayed through any ask
surface commits nothing.

**Registering a redirect URI with a provider** uses `redirect_uri`, joining the validated base
with `callback/<effective>` — the exact string the provider must be given, matched byte-for-byte
on the return leg, and the one place a malformed value could reach a third party.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/callback_http.py::make_callback_handler`
- `casa/rootfs/opt/casa/callback_http.py::make_done_handler`
- `casa/rootfs/opt/casa/callback_http.py::install_callback_log_redaction`
- `casa/rootfs/opt/casa/callback_http.py::OutcomeSampler`
- `casa/rootfs/opt/casa/callback_acks.py::CallbackAckStore`
- `casa/rootfs/opt/casa/callback_consent.py::render_callback_consent_message`
- `casa/rootfs/opt/casa/callback_reconcile.py::reconcile_plugin_callbacks`
- `casa/rootfs/opt/casa/callback_reconcile.py::compute_desired`
- `casa/rootfs/opt/casa/callback_urls.py::validated_base`
- `casa/rootfs/opt/casa/callback_urls.py::redirect_uri`
- `casa/rootfs/opt/casa/plugin_callbacks.py::parse_and_validate`
- `casa/rootfs/opt/casa/plugin_callbacks.py::ack_identity`
- `casa/rootfs/opt/casa/plugin_callbacks.py::declaration_digest`

**Tests**
- `tests/test_callback_http.py`
- `tests/test_callback_acks.py`
- `tests/test_callback_reconcile.py`
- `tests/test_callback_consent.py`
- `tests/test_plugin_callbacks.py`
- `tests/test_callback_urls.py`
- `tests/test_callback_reconcile_publication_fence.py`

**Related**
- [`architecture/callback-delivery.md`](../architecture/callback-delivery.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
<!-- END SOURCEMAP -->
