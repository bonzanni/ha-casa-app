---
last_reviewed: 2026-08-01
---

# The HTTP surface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The two HTTP applications the main Casa process runs, what each exposes, and how a request
from outside is authenticated before it can reach an agent. It does not cover what an agent
does once reached, nor the voice transport's own protocol beyond the point where a request
becomes a turn. The container also ships a third, separately supervised loopback HTTP
service — the MCP bridge — which belongs to `architecture/mcp-and-tools.md`, not here.

## Mental model

Routes are registered on **two separate applications**, and which one a route lands on
decides who can call it.

The **public app** carries the externally-reachable routes: a dashboard, a health endpoint,
agent invocation, the Telegram update sink, conditionally-registered voice routes, and
inbound webhooks — which are a *single wildcard route* backed by a dynamically-maintained
trigger allowlist, not per-trigger route registrations. (MCP and hook resolution are not
on this app: they are served by the standalone loopback bridge `svc-casa-mcp`, which
belongs to `architecture/mcp-and-tools.md`.) This document does not enumerate the routes; the registration block is the
authority and it changes.

The **internal app** carries routes intended for other processes in the container to call —
admin reload, personality and specialist endpoints, and a family of internal channel routes.
Its listener configuration, not its route table, is what makes it internal; check the runner
setup before assuming reachability either way. The `/admin/*` family carries a second gate on
top of the socket's filesystem permissions: a middleware reads the connecting peer's uid via
`SO_PEERCRED` and refuses anyone who is not root, so a same-container non-root identity that
reached the socket still cannot drive a reload — the channel-forwarding `/internal/*` routes
are deliberately *not* gated this way, because their forwarder is not root and they are
authorized per-engagement one layer up (INV-HTTP-007).

**nginx runs two listeners with different security postures, and conflating them is the
easiest way to be badly wrong here.** The Home Assistant ingress listener carries a
server-scope source restriction to the supervisor address. The second listener is published
by the app manifest as an external API port — declared with host publication *disabled* by
default, so it is host-reachable only where the operator maps it — and carries **no source
restriction at all** —
it proxies to the same backend application. So "reachable through the host" is true of one
listener and not the other, and a route's exposure depends on which listener you arrive on.

The listeners differ in two ways, and both matter: the ingress listener carries the
source restriction above (and sits behind Home Assistant's own authentication), while the
external listener carries neither — and a set of explicit 404s on the external listener
refuses a handful of path prefixes that the ingress listener passes through. For deciding
*which backend routes the external listener proxies*, the 404 set is the boundary. Read the
server blocks before reasoning about who can reach what.

Authentication is **per route**, not ambient. There is no boundary that authenticates
everything arriving on the public app, so the question for any route is which check *it*
performs — and several routes perform none.

**The routes with no application-layer check are the ones to understand first.** The MCP
and hook-resolution endpoints live on the loopback-only `svc-casa-mcp` bridge, not on this
app — an engagement-identity *claim* on them is credential-verified (INV-MCP-004,
INV-MCP-006), but the routes themselves demand nothing to be called and are contained by
the container wall alone. The external nginx listener still 404s the `/mcp/` and `/hooks/`
prefixes as defense in depth. A new route performing no check inherits no protection from
any of this; it inherits only its own absence of a check.

## Contracts & invariants

**INV-HTTP-001**: `verify` authenticates webhook-trigger requests under one of three named modes — a body HMAC, a static header, or a timestamped HMAC — and returns a single boolean.

Not "webhooks use HMAC": a static-header trigger is authenticated by comparing a shared
value, with no HMAC involved. Which mode applies is configuration, so read the trigger's
mode before reasoning about what protects it.

Scope matters as much as the modes. **`verify` is the webhook-trigger verifier, not the
application's authentication layer.** Agent invocation, the voice transports and the
Telegram update sink each perform their own route-specific check against the one shared
configured webhook secret. Do not read
this invariant as describing what protects any route other than a webhook trigger.

**INV-HTTP-002**: Every secret comparison inside `verify` uses a constant-time primitive, and an absent or empty secret returns false rather than passing.

Fail-closed on a missing secret is the part worth remembering: within this verifier there
is no path that passes when configuration is incomplete. That is a statement about
`verify`, not about the application — routes that never call it are unaffected by it.

**INV-HTTP-003**: No mode prevents replay. The timestamped mode *bounds* it to a tolerance window; the other two accept a valid credential indefinitely.

This is the correction most worth reading carefully, because the intuitive reading of
"timestamped" is wrong. Nothing tracks nonces or spent signatures, so a captured
timestamped request can be replayed **repeatedly within its window**, and a timestamp
modestly in the future is accepted — the comparison is on absolute difference. Bounded
replay is a materially weaker property than replay prevention.

The other two modes have no bound at all. A captured body-HMAC signature authenticates its
own exact body for as long as the secret is accepted; it cannot authenticate a modified
body, which is a real but narrow protection. A static header is a bearer token: no body
binding, no nonce, no expiry.

The tolerance default is a literal in the code, not an absent value the operator must
supply, and configured values are constrained to a bounded range. Replay is in the threat
model for every mode; choosing a mode chooses how long the window stays open.

The secrets themselves have a lifecycle worth knowing, and the two halves of the surface do
not share one. **Plugin** trigger secrets are minted *per trigger identity* — bound to the
plugin artifact, so a plugin update means a new identity and a fresh secret — and are
retired when the artifact's grant is revoked, so a later artifact cannot inherit a
credential. **Resident** trigger secrets are minted per NAME, when the trigger is
registered: at boot and on every reload that installs triggers, never on the request that
verifies one. Nothing retires a resident secret, so a trigger recreated under an old name
inherits the old credential; the names are globally unique, and a name is the whole
identity.

Two things this paragraph used to claim, and the code does not do. There is **no
dual-accept window**: the verifier takes a single secret, and the rotation state machine
has no caller outside its own tests. And a resident credential **can** survive a
declaration change — a casa-minted token also satisfies the provider validation rule, so
flipping `secret_owner` carries the live value over rather than replacing it. Changing the
owner of an existing trigger is not supported; delete it and create it under a new name.

The invoke route's concrete contract is easy to guess wrong: the global rate limit runs
*before* authentication; no configured secret is a 403 and a failed body-HMAC a 401; only
agents declaring the webhook capability are reachable; and the payload's `chat_id` decides
session reuse — two invocations with the same chat id share a conversation. That rate
limiter is one shared token bucket across the invoke *and* webhook surfaces, answering 429
with a Retry-After — a noisy webhook source throttles direct invocations too. The
per-minute caps are environment-tunable per ingress class: `WEBHOOK_RATE_PER_MIN`
(default 60), `TELEGRAM_RATE_PER_MIN` (30) and `VOICE_RATE_PER_MIN` (20), with zero
disabling that limiter.

**INV-HTTP-004**: External context arriving on a request cannot set provenance fields; the ingress supplies them.

A payload that could name its own origin could claim any origin, and provenance is what
later decides what a turn may do — see `provenance.py` for what is stripped.

**INV-HTTP-005**: The ingress-identity table is validated at boot against an independently-written route contract, and any disagreement between the two is a boot failure — as is a route that breaks either structural rule the check applies on top of that comparison: peer-namespace containment, and the Telegram surface and the human peer strategy implying each other.

Read the scope precisely, because the useful-sounding version of this is false. **The check
compares two hand-maintained declarations with each other. It does not inspect the
registered HTTP routes.** Adding a turn-producing route without touching either declaration
therefore produces no boot failure — the guarantee is that the two declarations cannot
drift apart, not that they cover the application. Keeping a new ingress honest is still a
matter of remembering to declare it.

What the check does enforce is worth having: both directions of set equality, so neither
declaration can gain or lose a route alone, plus per-route agreement on surface,
authentication flag and peer strategy.

Two structural rules are checked *beyond* that agreement, and they exist because a
coordinated edit to both declarations agrees with itself and so slips past the comparison.
The first is namespace containment: a human route's peer must resolve inside the human
namespace and every other route's must resolve outside it. The second ties the surface to
the strategy as a biconditional — the Telegram surface and the human peer strategy imply
each other. The second is the load-bearing one for person-vs-machine classification, and
the reason is easy to get backwards: **the peer bounds what a turn is called, but the
SURFACE is what decides whether it is recorded as a person.** `UserProvenance.from_origin`
derives the speaker kind from the surface, so a route wearing surface `telegram` with a
fixed machine peer satisfies every namespace rule and is still persisted as a user. The
namespace guard alone prevents a machine from *claiming a person's peer*; it does not
prevent it from being classified as one.

Per request the identity function raises instead of returning anything a caller could
mistake for "no identity" — there is no quiet fallback to an anonymous or system speaker.

**INV-HTTP-006**: Every accepted Telegram sender resolves to its own namespaced peer; private clearance is granted per sender — only the sender whose id matches the configured operator chat id keeps it, any other sender is floored to public — and a sender-less Telegram turn raises rather than borrowing an identity.

**No peer names the operator.** The route used to fix the operator's peer as a hardcoded
constant, so with the accept-all configuration any Telegram user's turns were recorded
under the operator's identity and ran at the operator's private recall clearance. #336
fixed the clearance half per sender; the peer half was fixed by deleting the constant
outright — the operator is `telegram:<their id>` like everyone else, and *clearance* is
the only thing that distinguishes them. The two consequences to hold on to are that
"who is the operator?" is now a fact about `telegram_chat_id` rather than about the
identity module, and that the boot check can no longer compare a peer against a literal:
it checks the **namespace** instead, in both directions — a human route must resolve
inside `telegram:`, every other route must resolve outside it, and the human and
`webhook:` namespaces must be prefix-disjoint so no trigger name can compose into a
person's identity.

What it does not cover: it names the *configured* operator. With `telegram_chat_id` empty
there is no configured operator identity, so no sender — the operator included — receives
operator attribution, and protected plugin tools are denied for every sender
(INV-PLUG-007); a group-id configuration names no sender at all. Sender identity is
Telegram's authentication of its user ids, not an additional Casa-side proof. Nor does
the namespace check bound what a *peer already recorded in memory* means: peers are the
key `content_document_id` hashes, so changing how a sender is named re-keys that sender's
documents rather than migrating them.

**INV-HTTP-007**: On the internal socket, `/admin/*` requests are refused unless the connecting peer is uid 0; an unreadable peer identity is refused, not admitted.

The gate exists because the socket's own filesystem permissions answer "who outside the
container" but not "which identity *inside* it": the container drops per-engagement work to a
non-root uid, and an in-container non-root process that reached the socket would otherwise
inherit the full admin surface. `SO_PEERCRED` reports the kernel-recorded credentials of the
peer at connect time on an `AF_UNIX` stream — an unprivileged client cannot forge it — and the
middleware fails **closed**: no transport, no socket, or a `getsockopt` error is treated as
non-root, because an identity that cannot be read is not proof of root. The gate is scoped to
the `/admin/*` prefix; the `/internal/*` forwarding routes pass through it untouched, since the
legitimate forwarder runs non-root and those routes carry their own per-engagement
authorization. `casactl`, the only legitimate `/admin/*` caller, runs as root and passes.

The related exposure worth holding next to this: the optional web terminal (`svc-ttyd`) is an
unauthenticated, writable **root** shell. It is bound to a root-restricted UNIX socket, not TCP
loopback, precisely so a dropped-uid engagement cannot reach it by connecting past nginx — see
"Failure behavior" and `architecture/overview.md` for the terminal's own boundary.

## Failure behavior

**A signature is absent, malformed, out of tolerance, or wrong.** `verify` returns false in
every case. What the caller sees is the handler's decision, not this function's — read the
handler for the response.

**A secret is missing or empty.** `verify` returns false before doing anything else.

**The health endpoint** returns a fixed ok response without consulting agents or memory. It
reports that the process is serving requests and nothing more; treating it as a system
health signal reads more into it than it carries.

## Extension points

A new public route means choosing its authentication explicitly **and** knowing which
listeners will carry it. Nothing authenticates it for you, so a route with no check is
callable by whatever can reach the app — which, on the external listener, is whatever can
reach the published port. If the route should not be externally reachable, the 404 list on
that listener is what excludes it, and adding the route alone does not add the exclusion.

A route that produces a turn also needs an entry in both ingress-identity declarations.
Nothing detects its absence at boot, so this is a step to remember rather than one the
system enforces.

The authorization-callback route (`GET /callback/{name}`) is a public, unauthenticated
route family that deliberately produces *no* turn — no ingress-identity row, no clearance,
no provenance — because a browser redirect is not an authenticated principal; it deposits
into a spool a plugin later collects. It therefore needs no ingress-identity declaration,
and it carries its own hazard instead: the query string is a bearer credential, so the
access logger suppresses the query for the `/callback/` prefix and the in-container nginx
`map` rule does the same. A new public route that carries a credential in its URL inherits
neither suppression automatically. See `architecture/callbacks.md`.

A new internal route belongs on the internal application, and is worth writing as though it
were reachable from outside — a later change to the listener is all it would take.

`read_secret` is the read path for webhook secret material, with validation and orphan
sweeping alongside it. Whether every secret in the system flows through it is not something
this document establishes; check the call sites for the one you care about.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/webhook_auth.py::verify`
- `casa/rootfs/opt/casa/webhook_auth.py::read_secret`
- `casa/rootfs/opt/casa/casa_core.py::healthz`
- `casa/rootfs/opt/casa/ingress_identity.py::validate_ingress_identity_table`
- `casa/rootfs/opt/casa/ingress_identity.py::ingress_identity`
- `casa/rootfs/opt/casa/provenance.py::sanitize_external_context`
- `casa/rootfs/opt/casa/internal_handlers.py::admin_peercred_middleware`
- `casa/rootfs/etc/s6-overlay/scripts/setup-nginx.sh`
- `casa/config.yaml::ports`

**Tests**
- `tests/test_webhook_auth.py::test_hmac_body_valid`
- `tests/test_webhook_auth.py::test_hmac_body_wrong_secret_fails`
- `tests/test_webhook_auth.py::test_hmac_body_missing_header_fails`
- `tests/test_webhook_origin_containment.py`
- `tests/test_setup_nginx_ingress.py`
- `tests/test_admin_peercred_gate.py`
- `tests/test_svc_ttyd_socket.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
