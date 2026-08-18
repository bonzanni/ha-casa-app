---
last_reviewed: 2026-08-18
---

# Callback delivery

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The delivery half of the authorization-callback facility: the on-disk spool a provider's
result is deposited into, the per-flow attempt ledger and its ack protocol, the consumer
verbs (`mint`, `collect`, `ack`), and the worker that redelivers a nudge until receipt.
The public `GET /callback/<name>` endpoint, the consent that opens a route, the
reconciler that routes it, and the validated redirect base URL are
[`architecture/callbacks.md`](callbacks.md). It does not cover what the collecting turn
then does with the code.

## Mental model

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
| `.removals/<plugin>-<uuid>.json` | casa | casa | casa (pruned after the note; never age-pruned while un-noted) |
| `ready.json`, `.index/*` | casa | casa | casa |

## Contracts & invariants

**INV-CB-002**: A pending state is consumed at most once — the claim-by-rename is the consumption point — and a replayed redirect never rewrites or duplicates a result.

Enforced in the spool: a claim renames `pending/<hash>` into `.claims/` and a result is
`link(2)`-published, so a second arrival for the same state finds no pending to claim and a
result that already exists, and mutates neither — under concurrent threads and processes alike.

What it does not cover: it bounds duplication *inside the spool*. A consumer that collects a
result and then acts non-idempotently owns that idempotency itself.

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
- `casa/rootfs/opt/casa/callback_episodes.py::kick`
- `casa/rootfs/opt/casa/callback_episodes.py::recovery`

**Tests**
- `tests/test_callback_spool.py`
- `tests/test_callback_attempts.py`
- `tests/test_callback_episodes.py`

**Related**
- [`architecture/callbacks.md`](../architecture/callbacks.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
