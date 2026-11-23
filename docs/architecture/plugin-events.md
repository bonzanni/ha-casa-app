---
last_reviewed: 2026-08-05
---

# Plugin-emitted domain events

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The facility that lets an installed plugin declare and emit a named domain event —
"something happened in my data" — and casa deliver it as a fresh headless turn to every
other installed plugin that declared a subscription on it and holds operator consent. It
covers the manifest surface (`casa.emits`/`casa.subscribes`), the on-disk emission spool
and its casa-minted generation protocol, the per-subscriber delivery ledger and
redelivery ladder, the operator consent that opens a route, the reconciler that computes
and publishes the routing map, the delivery worker, and the `ack_event`/
`event_ack_revoke` tools. It does not cover payload delivery (an event carries no data
across the wake), cross-plugin RPC, or the authorization-callback facility — its nearest
sibling, whose filesystem discipline this facility mirrors throughout.

The spool itself — the names it holds, generation folding, the delivery records and the
redelivery ladder — is [`plugin-event-spool.md`](plugin-event-spool.md).

## Mental model

**An event is a wake-up, not a data channel — level-triggered by design.** The
subscriber's own durable state is the real queue; an emission is a pure "something
happened" signal with no payload. A lost, suppressed, coalesced, or duplicate wake costs
promptness, never correctness — every accepted residual below leans on that doctrine, and
it is why fold recovery is idempotent replay rather than a write-ahead-then-delete dance.

**`casa.subscribes` is the edge, and consent binds the whole delivery, not just a route.**
Unlike a callback (which grants no turn or memory access), a subscription reaches into a
subscriber role — so the operator's ack binds `(subscriber, subscriber artifact id,
emitter, event, declaration digest, sorted delivery targets)` as one identity
(`ack_identity`). A plugin upgrade or a retargeted assignment mints a new identity, so
neither can silently carry an old consent forward; the emitter's own artifact id is
deliberately excluded, so an emitter-side upgrade never forces re-consent on its
subscribers. Because that identity composes a manifest declaration with the delivery
targets the registry assigns, the reconcile pass reads both from one pinned registry
snapshot — the same rule the trigger and callback reconcilers follow
([`plugin-setup.md`](plugin-setup.md)).

**Delivery is LLM-mediated through casa's own `ack_event` tool — a deliberate trade-off,
bounded by level-triggering.** There is no file-protocol ack a subscriber process claims
directly (the first subscriber is skill-only, with no other way to signal completion): the
wake instruction asks the agent to process the event and call `ack_event` with the given
token, then close the turn with the `<silent/>` sentinel — an event wake is a background
turn, so its tokens are buffered rather than streamed (the reminder-delivery convention)
and a close of the sentinel alone is suppressed instead of landing in the operator's chat.
Narration written before that closing sentinel on a wake that sent nothing still arrives,
without the tag: `ack_event` is bookkeeping, not a send (INV-OUT-006, the ruling on
#1075). If
the agent never acks — a crash, a bug, a forgetful agent — the ladder simply
exhausts after six accepted dispatches instead of hanging forever, and the exhaustion note
tells the operator so. The trade-off is accepted because a missed ack costs promptness,
not data: the subscriber's next independent wake still finds the same durable state.

**The spool is a same-uid protocol, not an inter-plugin security boundary** (callbacks
doctrine parity). Every plugin process runs as root, the same uid as every other in-process
attachment, so `/data/events` guards against itself racing or crashing within a shared-uid
process family, not against a hostile co-tenant of it.
Accepted
residuals, each bounded by level-triggering to lost promptness or a spurious-but-inert
wake: same-uid forgery of an emission file for an already-consented pair; token theft or
record rewrite to suppress or falsify a receipt; same-role token confusion by the model
dispatching two subscribers' wakes concurrently; the documented state-corruption double
fault producing one duplicate wake. An ack token is bearer material for exactly one
pending delivery, handled like a callback's query string — never logged, never echoed
beyond naming the acked subscriber.

## Contracts & invariants

**INV-EV-001**: Only an operator-consented, currently-reconciled route ever dispatches an event wake, and every unrouted pair's queued emissions are deleted every authoritative pass.

Enforced in two places. The sweep's watermark deletes every emission file of a pair with
no routed subscriber on every authoritative pass — not a TTL (`_sweep_emissions`). And
immediately before every dispatch attempt, under the same
asyncio dispatch-admission lock a revoke uses, `event_episodes._gate_ok` recomputes the
full consent identity from the *live* resolved manifest (declaration digest, artifact id,
sorted targets), requires it to equal the routed snapshot, and requires that identity be
present in the live ack store — a mismatch defers with no budget spent and kicks a
reconcile. When the reconciler's compute fails or the registry snapshot is invalid, it
publishes the typed `ROUTING_UNAVAILABLE` sentinel rather than an empty map; under the
sentinel the worker dispatches nothing and the spool's fold and destructive sweep both
become strict no-ops, so a transient failure suspends delivery without destroying a
single queued emission or delivery record.

What it does not cover: the watermark is pair-level, not per-emission — once any
subscriber is consented, a later subscriber may be woken by an emission predating its own
consent (harmless; the wake carries no data).

**INV-EV-003**: A subscription's consent binds subscriber, subscriber artifact id, emitter, event, declaration digest and sorted delivery targets as one identity, and any change to them unroutes the pair without resurrecting it later.

Enforced by `ack_identity` folding all six into the hash the operator approves and the
reconciler recomputes every pass, by `EventAckStore`'s whole-store fail-closed load (any
malformed record, or any stored key not equal to its own recomputed identity, yields zero
acks — never a partial store), and by the sweep terminalizing an unrouted pending record
as a tombstone (`revoked` if the subscriber is still installed, `removed` if not) rather
than deleting it outright. Fold's REPAIR step never recreates a record for a cohort
member that sweep already dropped — only a fresh OPEN, driven by the live routed set at
that moment, may re-admit a returning subscriber, and only after a genuine new consent.
The artifact id hashes repository, revision, subdirectory and name, so returning to a
revision recomputes its identity; leaving an artifact therefore revokes the subscriber's
event consents explicitly (`revoke_and_unroute`), as its trigger consents are — on
`plugin_remove`, and on a `plugin_update` that changes the artifact — and a reinstall or an
update back asks again (#1657). The reconcile's prune is not relied on: any resolver issue
in the registry skips it.

What it does not cover: the emitter's own artifact id is deliberately excluded from the
identity — an emitter-side upgrade that leaves its declared event name unchanged never
forces its subscribers to re-consent.

An approval also **rewrites the stored health report**, on both the success and the failure
path of the reconcile it fires. The `event_pending_ack` rows are recomputed statelessly from
the live ack store, but the report a tool reads is only rewritten where something asks for
it, so without this an approved subscription kept telling the operator it was waiting for
their approval — while it was already delivering — until an unrelated plugin mutation, reload
or reboot happened by. That is worse than a stale file: the assistant's read-only status tool
reports from it, so it contradicted the re-prompt tool, which correctly found nothing
pending. The failure path owes the same rewrite because the ack is durable before the
reconcile runs, so a compute failure there would leave the report saying "waiting for
approval" while the consent message says delivery could not be started. The regeneration
runs after the reconcile lock is released and holds the plugin-tools guard, since the report
lock orders the write and not the computation before it — the trigger and callback consent
paths do the same.

**INV-EV-005**: An ack token, the emission envelope, and the mint payload never reach a log line, a health surface, an operator notice, or a tool reply beyond naming the acked subscriber, and an ack mutates a record only on a live token match at that record's current generation.

Enforced end to end: `ack_event`'s handler interpolates the token into no reply field, no
log call and no exception message on any path (verified by a caplog sweep across
distinct token values); the emission envelope accepts exactly `{"v": 1}` and nothing
else — there is no forward-compatible payload channel to interpret at all, unlike a
callback's `meta`; and `EventSpool.ack` matches a presented token against the *current*
record's `ack_token` only, so a fold that rotates a stale generation's token makes the old
token a `no_match` rather than a stale-but-accepted ack (a compare-and-swap by
construction, not by an explicit version check).

What it does not cover: this is a guarantee over casa's own surfaces. A subscriber agent
that itself echoes the token elsewhere — into its own memory, into a reply it composes —
is outside casa's control, a consumer obligation, not an enforced one.

**INV-EV-006**: An event-dispatched turn is marked as an internal wake by a context key no external ingress can set, and an interactive input request inside that turn is mechanically refused.

Enforced by `provenance.RESERVED_CONTEXT_KEYS` including `synthetic` — the exact key
`event_episodes`'s `_wake_context` sets to the literal `"event_wake"` — so
`sanitize_external_context` strips that key from anything an inbound webhook or `/invoke`
body could forge, leaving every other field of an attacker-supplied context untouched.
The `ask_user` tool's provenance classifier reads that marker off the turn's real origin
and refuses the call outright when it is present, mirroring the same mechanical rejection
the setup and callback wake markers already rely on.

What it does not cover: the gate is specific to `ask_user`; the wake instruction's own
prose ("do not ask... record it durably and end the turn") is what asks a subscriber's
tool logic to behave headlessly everywhere else, and that is a courtesy the instruction
states, not a mechanically enforced one beyond the one tool this invariant pins.

**INV-EV-007**: An operator note the worker attempts while the Telegram channel is not ready, before the channels have finished starting, is logged at INFO without a traceback, and an owed exhaustion or removal note stays owed; every other failed note keeps an ERROR with its traceback.

Enforced by type: `operator_notify` raises `OperatorNotifyBeforeStart`, a `RuntimeError`
subclass, for a not-ready channel only until `ChannelManager.start_all` has returned; the
exhaustion, removal and advisory note sites catch it before their broad handler, with the
same control flow (un-noted, no mark).

What it does not cover: "started" means `start_all` returned, not ready — a first bring-up
that failed and returned keeps ERROR, reported until the channel recovers.

## Failure behavior

**A reconcile compute fails, or the registry snapshot itself is invalid.** The published
map becomes the `ROUTING_UNAVAILABLE` sentinel, never an empty map — an empty map is an
authoritative result that licenses the destructive sweep; the sentinel licenses none of
it. Dispatch stops entirely, fold is a strict no-op in every phase, and sweep degrades to
part-TTL housekeeping only. The removal-note and exhaustion-notice scans still run — they
read settled records and flip `noted` on a confirmed send, nothing destructive or
forward-moving, so an owed operator notice never waits on routing health. Routing and
full sweeping resume in full the instant a later
compute succeeds, and every reconcile call — successful or fail-closed — kicks the worker
so it re-evaluates promptly.

**The ack store is missing, unreadable, or malformed.** Treated as zero acks store-wide;
every subscription stays `event_pending_ack` until a fresh operator approval rewrites a
valid store. It never raises into the reconciler.

**A delivery nudge kick is lost to a crash.** It is a hint only: the worker's timed wake
recomputes the nearest due `next_nudge_ts` from the ledger every pass regardless, and
every reconcile call kicks broadly on success or failure alike, so delivery converges on
the durable schedule, never a request-path signal.

**A dispatch bus rejects an attempt.** Retried up to three times in-pass with a
(1.0 s, 5.0 s) backoff; if every retry is rejected, the record defers on an escalating
capped delay and spends no budget — an unavailable bus yields a bounded cadence, never a
spin or a spent chance.

## Extension points

**A new emitted event** is a `casa.emits` entry — `{"name": "<event>"}` — parsed and
intrinsically validated the same way a callback name is; it grants no turn or memory
access by itself. **A new subscription** is a `casa.subscribes` entry naming the emitter's
registry name and event; it is the thing that needs operator consent, because unlike a
callback it wires a delivery into a role. Both blocks are per-plugin all-or-nothing: one
malformed entry darkens the whole block.

**A single-subscription off-switch** is the `event_ack_revoke` tool: it unroutes one
`(subscriber, emitter, event)` pair (all of a plugin's, without `emitter`/`event`) from the
published map before deleting its ack record, under the pre-send gate's
dispatch-admission lock, so no dispatch is admitted once the call returns, and
records the revoke as a Deny in `consent_denials` (a removal or update records none). A
denied or revoked subscription is off on its own (unrouted, no `event_pending_ack` row),
and a Deny tap reconciles at once, so the plugin's other subscriptions keep routing; an
unanswered one holds back the set. A plugin mutation or reload re-asks it;
`consent_reprompt` ([`plugin-mutation-tools.md`](plugin-mutation-tools.md)) skips it. The
registry is in memory: after a restart the subscription reads as unanswered.

**Explicitly out of scope** (future hardening, not present today): casa-brokered emission
(nothing yet lets casa compose or filter an emission on its way through), per-plugin
OS-level isolation (the same-uid threat model above), and payload-bearing events — the
level-triggered doctrine forbids carrying data across the wake at all; a subscriber that
needs data reads it from its own state, not from the event.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_events.py::ack_identity`
- `casa/rootfs/opt/casa/plugin_events.py::parse_and_validate_emits`
- `casa/rootfs/opt/casa/plugin_events.py::parse_and_validate_subscribes`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.sweep`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.ack`
- `casa/rootfs/opt/casa/event_spool.py::emit`
- `casa/rootfs/opt/casa/event_acks.py::EventAckStore`
- `casa/rootfs/opt/casa/event_consent.py::render_event_consent_message`
- `casa/rootfs/opt/casa/event_reconcile.py::compute_desired`
- `casa/rootfs/opt/casa/event_reconcile.py::reconcile_plugin_events`
- `casa/rootfs/opt/casa/event_reconcile.py::revoke_and_unroute`
- `casa/rootfs/opt/casa/event_episodes.py::configure`
- `casa/rootfs/opt/casa/event_episodes.py::_gate_ok`
- `casa/rootfs/opt/casa/plugin_dispatch.py::compose`
- `casa/rootfs/opt/casa/tools.py::ack_event`
- `casa/rootfs/opt/casa/tools.py::event_ack_revoke`

**Tests**
- `tests/test_plugin_events.py`
- `tests/test_event_spool.py`
- `tests/test_event_acks.py`
- `tests/test_event_consent.py`
- `tests/test_event_reconcile.py`
- `tests/test_event_episodes.py`
- `tests/test_tools_ack_event.py`
- `tests/test_event_wiring.py`
- `tests/test_event_acceptance.py`

**Related**
- [`architecture/callbacks.md`](../architecture/callbacks.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/plugin-event-spool.md`](../architecture/plugin-event-spool.md)
<!-- END SOURCEMAP -->
