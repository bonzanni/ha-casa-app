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

## Mental model

**An event is a wake-up, not a data channel — level-triggered by design.** The
subscriber's own durable state is the real queue; an emission is a pure "something
happened" signal with no payload. A lost, suppressed, coalesced, or duplicate wake costs
promptness, never correctness — every accepted residual below leans on that doctrine, and
it is why fold recovery is idempotent replay rather than a write-ahead-then-delete dance.

**casa mints every generation; an emission only ever proposes one.** An emitter calls
`emit()` to drop a fact-free file into its own `emissions/` directory; casa alone decides
when those queued files become a delivery. Once the pair is idle (no subscriber still
holds a pending record), fold mints one new generation from up to `FOLD_BATCH_MAX = 64`
oldest emissions, mints one delivery record per currently-routed subscriber, and unlinks
the folded files — but only once every fan-out member has a durably written record, so a
partial write never destroys an undelivered wake. An emission racing an in-flight
generation never folds into it; it waits, coalesced, for the generation after.

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
and a narration-only close is suppressed instead of landing in the operator's chat. If
the agent never acks — a crash, a bug, a forgetful agent — the ladder simply
exhausts after six accepted dispatches instead of hanging forever, and the exhaustion note
tells the operator so. The trade-off is accepted because a missed ack costs promptness,
not data: the subscriber's next independent wake still finds the same durable state.

**The spool is a same-uid protocol, not an inter-plugin security boundary** (callbacks
doctrine parity). A resident's or specialist's plugin processes still run as root, same as
every other in-process (`in_casa`-driver) attachment; a `claude_code`-driver (executor)
engagement's `--plugin-dir`-attached plugins instead run under that one engagement's
containment-stage-2 dropped uid, not root — but that is still one uid shared by every
plugin *within* that engagement, so `/data/events` still guards against itself racing or
crashing within a shared-uid process family, not against a hostile co-tenant of it.
Accepted
residuals, each bounded by level-triggering to lost promptness or a spurious-but-inert
wake: same-uid forgery of an emission file for an already-consented pair; token theft or
record rewrite to suppress or falsify a receipt; same-role token confusion by the model
dispatching two subscribers' wakes concurrently; the documented state-corruption double
fault producing one duplicate wake. An ack token is bearer material for exactly one
pending delivery, handled like a callback's query string — never logged, never echoed
beyond naming the acked subscriber.

Who owns each name in a plugin's spool (`/data/events/<emitter>/`, default root; every
dot-prefixed root entry is casa-reserved):

| Path | Created by | Mutated by | Deleted by |
|---|---|---|---|
| `.dir-id` | casa (`ensure_emitter_dirs`) | casa (repair, on detected corruption) | casa only — whole-tree orphan GC |
| `ready.json` | casa (`write_ready`) | casa | casa (`delete_ready`, or orphan GC) |
| `emissions/<event>--<u32hex>.json` | emitter (`emit()`) | nobody | casa — fold (complete fan-out) or sweep (watermark/valve/unfoldable) |
| `emissions/.part-<u32hex>` | emitter (`emit()` staging) | nobody | emitter (rename to final) or casa (`TEMP_TTL_S` sweep) |
| `state/<event>.json` | casa (fold OPEN) | casa (fold OPEN/REPAIR/RECONSTRUCT) | never — retained for the emitter directory's whole lifetime |
| `state/.corrupt-<ts>-<event>` | casa (RECONSTRUCT quarantine) | nobody | casa only — whole-tree orphan GC (evidence, not pruned individually) |
| `delivery/<event>--<subscriber>.json` | casa (fold REPAIR/OPEN) | casa (nudge/accept/defer, `ack`, sweep terminalize) | casa — after a durable `.removals` record names it |
| `delivery/.corrupt-<ts>-<name>` | casa (sweep quarantine) | nobody | casa — after a durable `.removals` record names it |
| `.removals/<plugin>-<uuid>.json` | casa | casa (`mark_removal_noted`) | casa — pruned 7 days after noted (30-day hard bound, noted records only; an un-noted record is a notice still owed and is never age-pruned) |

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

**INV-EV-002**: Fold mints at most one generation and one delivery ladder per idle pass, and no queued emission is ever lost to it.

Enforced by the fold's three phases run in order every pass — RECONSTRUCT (a missing or
corrupt state file with surviving delivery records rebuilds at `max(record gens)`,
quarantining the corrupt original), REPAIR (backfills a stale cohort member's record at
the current generation before any idle decision, and unlinks folded emissions only once
every eligible member's fan-out is provably complete), then OPEN (mints generation+1 once
idle, folding the oldest `FOLD_BATCH_MAX = 64` emissions — an emission arriving during an
open generation is never folded into it, so it survives untouched into the *next*
generation once idle again). A separate `MAX_EMISSION_FILES = 512` disk-pressure valve is
the sole documented exception, deleting oldest overflow at sweep with a log line.
Generations never roll backward while a pair's `state/<event>.json` survives.

What it does not cover: if that state file is lost while *no* delivery record survives
either — a state fault after normal subscriber removal, or an emitter reinstall under a
fresh `.dir-id` — the pair silently restarts at generation 1, indistinguishable on disk
from a brand-new pair; no issue is raised. And a crash between a lost state file and a
lost `folded` list can re-fold already-delivered emissions as a new generation — one
duplicate wake, bounded by level-triggering.

**INV-EV-003**: A subscription's consent binds subscriber, subscriber artifact id, emitter, event, declaration digest and sorted delivery targets as one identity, and any change to them unroutes the pair without resurrecting it later.

Enforced by `ack_identity` folding all six into the hash the operator approves and the
reconciler recomputes every pass, by `EventAckStore`'s whole-store fail-closed load (any
malformed record, or any stored key not equal to its own recomputed identity, yields zero
acks — never a partial store), and by the sweep terminalizing an unrouted pending record
as a tombstone (`revoked` if the subscriber is still installed, `removed` if not) rather
than deleting it outright. Fold's REPAIR step never recreates a record for a cohort
member that sweep already dropped — only a fresh OPEN, driven by the live routed set at
that moment, may re-admit a returning subscriber, and only after a genuine new consent.

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

**INV-EV-004**: Every delivery record eventually reaches `done` carrying exactly one durable outcome, redelivered on a fixed six-slot ladder until receipt or budget exhaustion.

Enforced by `update_delivery_nudge`'s conditional read-merge-write (refuses unless the
on-disk record is still `pending` at the exact `gen` the caller expects — a done record is
immutable, with exactly one sanctioned exception: `mark_delivery_noted` flips `noted`
False→True on a done record, still gen-matched, and mutates nothing else — and a rotated
generation refuses a stale caller outright) and by
`terminalize`, which returns a record already `done` completely unchanged rather than
re-stamping it. The ladder is `PHASE_OFFSETS = (0s, 5m, 30m, 2h, 6h, 24h)`, anchored on
the record's own `minted_ts`, budgeted at `MAX_NUDGES = 6` bus-accepted dispatches; a
bus-rejected attempt spends no budget and instead defers on an escalating capped delay.
The sixth accepted dispatch's exhaustion is a single conditional write setting
`status=done, outcome=exhausted, noted=False` together — never three separate writes. The
operator note is then delivered notify-then-mark, exactly like the removal notes: the
worker's exhaustion scan sends it through the observed notify seam (which raises when the
Telegram channel is absent or not yet started, rather than false-succeeding) and only a
confirmed send flips `noted`, retried every pass until it lands — so a boot-window send
failure costs delay, never the notice. A send that succeeded whose mark then fails is
keyed in memory and only the mark is retried, bounding duplicates to one per process
crash.

What it does not cover: acceptance and the ledger write recording it are not atomic with
each other — a crash between a bus accept and `update_delivery_nudge` costs one duplicate
wake on the next pass, bounded by level-triggering exactly like every other accepted
residual here.

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

**A state file is corrupt but delivery records survive.** Reconstructed at
`max(record gens)`; the corrupt original is quarantined under
`state/.corrupt-<ts>-<event>`, never silently overwritten. If the quarantine rename
itself cannot be proven durable, reconstruction — and therefore that pass's OPEN — defers
whole to the next pass rather than risk destroying the only evidence a corruption ever
happened.

**Both the state file and every delivery record are lost together.** The pair silently
restarts at generation 1, indistinguishable on disk from a pair that never existed before;
no issue is raised (an undetectable condition claims nothing it cannot prove).

**A delivery record is malformed** (read fully, content invalid). Quarantined under
`delivery/.corrupt-<ts>-<name>` and surfaced as `event_spool_issue`; it never blocks the
pair's idle decision, and its token is gone, so a later ack for it reads as `no_match`.

**A delivery record is unreadable** — a transient read failure (fd pressure,
`EMFILE`/`EIO`), distinct from malformed content actually seen and rejected. Fold defers
the WHOLE pair that pass: never idle, `fan_out_complete` never true, no reconstruct/
repair/open — an unreadable record must never fold into "absent" the way a genuinely
missing one does, since that would let a fresh generation open right over an in-flight
delivery this pass simply failed to see, rotating its token out from under it. Not
quarantined either (sweep applies the same defer); normal operation resumes the moment
the file reads back cleanly.

**The ack store is missing, unreadable, or malformed.** Treated as zero acks store-wide;
every subscription stays `event_pending_ack` until a fresh operator approval rewrites a
valid store. It never raises into the reconciler.

**A plugin is removed with deliveries in flight.** Its unrouted records were already
tombstoned by the previous sweep, never deleted outright. Removal instead inventories
every unsettled record and quarantined artifact for that subscriber, writes one
strict-durable `.removals` record naming them, and only then deletes the artifacts — no
purge proceeds on an unproven inventory. The worker turns each un-noted removal record
into one operator note by **notifying before marking**: only a confirmed send marks it
noted, so a crash there costs one duplicate note, never a silently dropped one — the same
ordering budget exhaustion now follows, both riding the observed notify seam. A note that
sent but failed to mark is keyed in memory so later passes retry only the mark.

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
`(subscriber, emitter, event)` pair — or every subscription a plugin holds, when
`emitter`/`event` are omitted — from the published map before deleting its ack record,
under the same dispatch-admission lock the pre-send gate uses, so no dispatch can be
admitted for it once the call returns. A later re-approval re-consents; the consent DM
re-prompts on the next reconcile, or on demand via the `consent_reprompt` tool — the
prompt-only re-issue shared by all three consent kinds (its contract is in
[`plugin-mutation-tools.md`](plugin-mutation-tools.md)), which skips subscriptions the
operator explicitly denied.

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
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.fold_pass`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.sweep`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.ack`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.update_delivery_nudge`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.gc_orphan_dirs`
- `casa/rootfs/opt/casa/event_spool.py::emit`
- `casa/rootfs/opt/casa/event_attempts.py::validate_record`
- `casa/rootfs/opt/casa/event_attempts.py::terminalize`
- `casa/rootfs/opt/casa/event_attempts.py::next_nudge_after_accept`
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
- `tests/test_event_attempts.py`
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
<!-- END SOURCEMAP -->
