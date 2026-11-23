---
last_reviewed: 2026-11-23
---

# The plugin event spool: generations, delivery records and the ladder

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The on-disk side of plugin-emitted domain events: who owns each name in an emitter's spool, how casa folds queued emissions into a generation, the per-subscriber delivery record and its redelivery ladder, and what a damaged state file, a damaged delivery record or a removed subscriber leaves behind. What an event is, the operator consent that opens a route, the reconciler, the delivery worker's gate and the `ack_event`/`event_ack_revoke` tools are [`plugin-events.md`](plugin-events.md).

## Mental model

**casa mints every generation; an emission only ever proposes one.** An emitter calls
`emit()` to drop a fact-free file into its own `emissions/` directory; casa alone decides
when those queued files become a delivery. Once the pair is idle (no subscriber still
holds a pending record), fold mints one new generation from up to `FOLD_BATCH_MAX = 64`
oldest emissions, mints one delivery record per currently-routed subscriber, and unlinks
the folded files — but only once every fan-out member has a durably written record, so a
partial write never destroys an undelivered wake. An emission racing an in-flight
generation never folds into it; it waits, coalesced, for the generation after.

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
The sixth accepted dispatch leaves the record pending with a final due time `FINAL_GRACE_S` (one hour) out, so that wake's turn can still ack; only when that slot passes unacknowledged does the worker exhaust it, in a single conditional write setting `status=done, outcome=exhausted, noted=False` together — never three separate writes — and then runs another pass at once so an event queued behind that wake opens. The
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

## Failure behavior

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

**A plugin is removed with deliveries in flight.** Its unrouted records were already
tombstoned by the previous sweep, never deleted outright. Removal instead inventories
every unsettled record and quarantined artifact for that subscriber, writes one
strict-durable `.removals` record naming them, and only then deletes the artifacts — no
purge proceeds on an unproven inventory. The worker turns each un-noted removal record
into one operator note by **notifying before marking**: only a confirmed send marks it
noted, so a crash there costs one duplicate note, never a silently dropped one — the same
ordering budget exhaustion now follows, both riding the observed notify seam. A note that
sent but failed to mark is keyed in memory so later passes retry only the mark.

## Extension points

The spool has none of its own: a new event or subscription is a manifest declaration, as [`plugin-events.md`](plugin-events.md) describes.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.fold_pass`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.update_delivery_nudge`
- `casa/rootfs/opt/casa/event_spool.py::EventSpool.gc_orphan_dirs`
- `casa/rootfs/opt/casa/event_attempts.py::validate_record`
- `casa/rootfs/opt/casa/event_attempts.py::terminalize`
- `casa/rootfs/opt/casa/event_attempts.py::next_nudge_after_accept`

**Tests**
- `tests/test_event_spool.py`
- `tests/test_event_attempts.py`

**Related**
- [`architecture/plugin-events.md`](../architecture/plugin-events.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
