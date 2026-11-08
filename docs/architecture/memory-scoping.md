---
last_reviewed: 2026-11-02
---

# Memory scoping

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Which stored facts a given reader gets back: the read-clearance model — per channel, per
Telegram sender via the stamped origin, failing closed — the ledgers Casa maintains, one per
clearance it serves, each written only from what that clearance can read, the check every
read path makes on each object it returns, the clearance an engagement
inherits from its creating turn and the monotonic clamp steering applies to it. How a
ledger is served, what a
caller is told when the store cannot answer, and what a caller may claim from what comes
back are [`architecture/memory-serving.md`](memory-serving.md) and
[`architecture/memory-tools.md`](memory-tools.md); write trust is
[`architecture/memory-capture.md`](memory-capture.md), retention is
[`architecture/memory-lifecycle.md`](memory-lifecycle.md), and tier
labelling is [`architecture/memory-labelling.md`](memory-labelling.md).

## Mental model

**Read clearance is per channel and fails closed** — and on Telegram, per *sender*. Known
channels are mapped explicitly; an unrecognised one gets the *least* sensitive clearance.
The fail direction is the security control here: an unknown surface must read less, never
more, and a test pins the function's docstring to that direction so prose and behaviour
cannot drift apart again. The Telegram channel's mapped clearance (private) is not
route-wide: the ingress stamps a per-sender origin clearance — private only for the sender
whose id matches the configured operator chat id, public for anyone else the accept-all
mode lets in — and origin-aware resolution honors that stamp, failing closed to public
when a telegram-marked turn carries a missing or malformed one. Both ways into a Telegram
turn stamp it: a message and a button tap.

**Each served clearance has a ledger of its own, written only from what it can read.** Memory
text served at a tier is generated from inputs readable at that tier; it is never a higher
ledger filtered on the way out, because a filter over entries cannot recover a restriction
the entry's writer never saw. So Casa maintains one ledger per clearance it serves:
`private` always, and `friends` while a role that reads at `friends` has a served-view
budget above 0 — the butler, since voice reads at `friends`. Which clearances have such a
reader is worked out once at boot from each role's declared ingresses and budget, so a
budget a reload changes counts from the next start. A session reads the ledger of the
highest maintained clearance at or below its own: `family` reads `friends`, `public` reads
none. The writer files each ledger as its own job, `private` first: that job classifies
each event, and a lower ledger is given only events classified at or below it, so its cost
scales with lower-tier content alone ([`architecture/memory-writer.md`](memory-writer.md)).
Ellen's own replies carry the clearance of the session that wrote them as their floor
([`architecture/memory-labelling.md`](memory-labelling.md)), so what reaches the butler's
ledger is what people said at `friends` and below, never Ellen's private replies.

**An update above a lower ledger takes the old value out of it.** Each event is classified
on its own, so a change can land above the fact it changes: "my pool locker is ZQXL5" at
`friends`, then "the locker is ZQXL7 now" at `family`. The `friends` job never sees the
update, and on its own it would keep ZQXL5 as current for the butler to serve. The `private`
job is the one place that sees both, since it reads every ledger and runs first. So it is
also given each lower ledger's current entries and the events still owed to that ledger, and
each of its own events carries the tier it is committed at. When an event it files replaces
or contradicts what a lower ledger holds or is about to file, and does not itself reach that
ledger, the model *withdraws* it there: the entry is retired, or the owed event is closed
with `no_change`. Nothing of the update goes below: the lower ledger simply no longer has the
old value, and its readers are told nothing rather than something stale. Which value is
stale is the model's judgement; the code checks only that each withdrawal names an entry or
event of that ledger that the run was given. The verbatim record is not changed: search and
date browse at `friends` still find the dated statement that was said.

**Every read path checks each object it returns.** The served view and `open_memory` read
the reader's ledger and nothing else; an object there is dated only from source events that
ledger can read. The correction note reads that ledger and re-checks each correction's own
tier. Search hits, each hit's window neighbours and date browse check every event's tier
before anything is paged or counted, so a count never reveals an unreadable event. An id
the reader's ledger does not hold gets
the same answer as an id that does not exist, whether it names an object of another ledger
or nothing at all.

**An engagement reads at the clearance of the turn that created it.** This needs stating
because the mechanism is not the obvious one: an engagement's own tool calls run bound to the
engagement record, and where the ambient turn origin they run under carries no route of its
own, a clearance resolved from that origin alone would find nothing and fall through to the
channel default, handing an engagement started by a low-clearance sender the operator's
private tier. The engagement's recorded origin carries the markers its creating turn was
stamped with, and that is what its reads resolve against: its own recall, the engager-side
context it can query, and a nested engagement it spawns, which inherits rather than
re-deriving. An engagement whose record predates this (or came from an origin that stamps no
route) keeps the channel-keyed behaviour.

Inheritance at creation is only half the rule, because an engagement is *steerable*: anyone
in the engagement supergroup can direct it by messaging its topic. So the recorded clearance
is also a **monotonic floor** — a steering turn clamps it down to that sender's clearance and
never raises it back, which makes the property "an engagement never reads above the
least-privileged person who has taken part in it". The clamp is deliberately one-way: two
people steering concurrently can only drive it further down, and the operator returning to
their own engagement does not restore what a lower-clearance participant has already seen.

A clamp that moves the record does not stop at future reads — it evicts what the session
already holds. The transcript was built at the old tier, and
a lower-clearance steerer could simply ask the engagement to restate them, so the same locked
ingress pass that lowers the record also: durably marks the context for rebuild (the flag and
the clamp persist in one write, so a crash between them cannot happen), withholds the
record's own launch materials (task, brief, context, world-state, a fresh job's recorded
launch context and the text of its pending completion, INV-BGJOB-007 — every later render re-derives from the record, and evicting the session
while the record still carried them would re-import them), tears the live session down, and drops the resume pointer — recording the evicted session on the record in that same write, so the terminal transcript pass (INV-ENG-027) deletes it once the engagement ends. Every
resume path — the steering turn itself, a system continuation — refuses to resume while the
rebuild is pending and establishes a fresh session at the clamped floor instead; while it is
pending, the old session's tool calls are refused at the in-process tool fence, so it cannot
read or launch children carrying its pre-clamp task. A read already in flight when the clamp lands is
re-filtered at the new clearance after it returns, and a launch caught mid-start aborts
rather than deliver a prompt rendered from pre-clamp materials.

The teardown never cuts an `in_casa` turn that is already running: it waits for that turn to
end. The wait does not run under the per-topic handler lock, so a `/cancel` still interrupts
the turn. When a turn is running, the ingress skips the eager teardown and the steering
message's delivery task waits for the turn, then runs the rebuild itself. That rebuild drops
the session pointer again, since the finished turn can have written it back. A turn that was
waiting for the turn lock when the clamp landed is refused before it is accepted and is sent
into the fresh session instead. A batch of a background job is therefore never ended by a
downgrade ([`background-jobs.md`](background-jobs.md)).

What the eviction deliberately does not do: content already posted to the topic stays
posted, and a turn already running at clamp time completes into the topic, its tool calls
refused while the rebuild is pending —
engagement topics are readable by every supergroup member regardless, so both are
disclosures the topic already carried. A nested engagement spawned before the clamp keeps
its own record's clearance (steering its topic clamps it the same way). And it only moves a
record that carries a stamped clearance: an engagement running before per-sender markers
existed has none, so it keeps channel-keyed clearance — on Telegram, private — until it
finishes.

## Contracts & invariants

**INV-MEM-003**: An unrecognised channel reads at the least-sensitive tier.

Enforced by the channel-clearance lookup's default. The direction matters: an unknown surface
sees less, not more.

What it does not cover: origin-aware resolution is narrower than it sounds. The served
memory view, the correction note and the memory tools resolve clearance from the stamped
origin (`clearance_for_origin()`), so a webhook turn's clearance there depends on its
declared origin. Delegated recall honours a route its caller passes, but falls back to
channel-keyed clearance for every caller that passes none — check the call site before
assuming a delegated read is origin-filtered.

**INV-MEM-029**: A writer job for ledger L is given only what is readable at L — the events pending in L, which below `private` are only events classified at or below it, a window of earlier events readable at L, and L's own objects (and, for a `private` filing job, each lower maintained ledger's current entries and the events still owed to it), all built by `assemble_input`, the only builder of a job's input — and its patches change objects of L only, each recording as its sources only events readable at L; its only write into another ledger is a withdrawal (INV-MEM-039).

Enforced by the pending view's eligibility rule, by input assembly reading the events, the
window and the objects (for `private`, also the lower ledgers) through L in one read transaction, by the patch loader refusing
an id that is no object of L, and by every object write in the commit dropping a source id
that names no event classified at or below L — one the model invented, since it was never
given such an event — without failing the job. The `private` job runs before any lower one, so an event
reaches a lower ledger only once classified. Pinned by a `private` canary in an event, in
the `private` ledger and in its history, and a provisional event, none of which may appear
in any `friends` prompt, object or revision, with every prompt the model port is given
checked against the input that function built for the running job.

What it does not cover: a source id naming a readable event the job was not given is kept
— it is readable at L either way; and a tier raised after a lower ledger filed the event,
since nothing reclassifies an event (an object citing it is then dated from nothing,
INV-MEM-034).

**INV-MEM-039**: The only write a writer job makes into a ledger other than its own is a `private` filing job's withdrawal into a lower maintained ledger L', and it carries nothing from above L': a new revision of a current L' entry the run was given that copies that entry's own state, retired, with the operation `withdraw` and no reason, or a `no_change` disposition in L' for an event pending there that the run was shown; every other field of a withdrawal is ignored, each target applies once, and a given entry whose revision or a given owed event whose correction mark changed since the run's read makes the run stale.

Enforced by the reply parser splitting every `withdraw` operation off before anything reads
the job's own operations, so a withdrawal can neither cite one of the job's events nor reach
the patch loader; by the commit reading only a withdrawal's ledger and its entry id or event
`seq`, against what the run's one read transaction gave it; and by the patch module's
withdrawal copying the entry's own row. A target the run was not given, an entry that is not
an entry, or an event no longer owed there is dropped with one warning, never a failed job.
A lower job's withdrawal is dropped the same way: it was given no lower ledger. Pinned by
the live case at each timing (filed in the same batch, filed earlier, still owed), and by a
withdrawal stuffed with an above-`friends` canary in every field and the update's own `seq`,
none of which may appear in any `friends` object, revision or disposition.

What it does not cover: whether the model withdraws the right thing — a missed withdrawal
leaves the old value, a wrong one removes a value the lower ledger could still serve. A
session already running keeps the view it was served at its start, as with any other change.
The owed events a run is shown are bounded at the ledger's whole-text bound (40 000
tokens), newest first; past it the oldest are left out and cannot be withdrawn. An entry
or event is withdrawn whole, with whatever else it held.

**INV-MEM-034**: Every memory read path checks each object it returns against the reader's clearance — the served view and `open_memory` by ledger, the correction note by ledger and each correction's own tier, search hits, their window neighbours and date browse by event tier before paging and counts — and an id the reader's ledger does not hold gets the same answer as an id that does not exist.

Enforced in each read function rather than by its callers, so a tool, the engager query, a
delegated run and a resident turn all inherit it. Pinned by a matrix of every path against
every reader clearance over one store holding a canary at every tier.

What it does not cover: the meter, which reports counts per ledger to the operator, and the
whole-store export, which is the operator's own copy.

**INV-MEM-008**: An engagement's reads resolve clearance from the origin markers its own record carries, and a steering turn from a lower-clearance sender clamps those markers down permanently.

Enforced in two places that have to agree: where the markers are resolved for an
access-control decision — the ambient origin wins when it has a route of its own (an
in-process delegated turn), otherwise the bound engagement record supplies them — and at the
engagement-topic ingress, which lowers the record's stamped clearance to each steering
sender's before their turn is delivered. Every read an engagement can perform resolves
through the first: its own recall, the engager-side context, and a nested engagement it
spawns.

What it does not cover: a record carrying no markers falls back to channel-keyed clearance,
which on Telegram is private — so this tightens what a *newly* stamped origin can reach, and
does not retroactively downgrade engagements created before the markers existed. The clamp
also cannot un-share what was already disclosed before a lower-clearance sender arrived.

**INV-MEM-011**: A clearance downgrade durably invalidates the engagement's session context in the same write that lowers the record — until a fresh session is established at the clamped floor, no path resumes the old session and the record's tool calls are refused at the in-process tool fence.

Enforced by the clamp setting the rebuild flag and withholding the record's launch materials
under one strict persist; by the resume core routing a flagged record to a fresh-session
rebuild instead of a resume; by the in-process tool fence refusing a flagged record; by the
`in_casa` turn lock, under which a waiting turn re-checks the flag and the live session
before it is accepted; and by the post-await re-filter and the driver's last-instant launch
gate, which close the calls already in flight when the clamp lands.

What it does not cover: output of a turn already running at clamp time, content already
posted to the (group-readable) topic, and a pre-clamp nested child's own record — the
deliberate residuals listed above. The completion tool stays reachable while the rebuild is
pending, its output being in-flight-turn residual.

## Failure behavior

**A clamp fails to persist.** The clamp is applied in memory first, so a failed persist
leaves this process correctly clamped while a restart would restore the higher tier
(logged as security-relevant).

A store that cannot be read is reported unavailable by every read path
([`architecture/memory-tools.md`](memory-tools.md),
[`architecture/memory-store.md`](memory-store.md)); failures on the write side are
[`architecture/memory-capture.md`](memory-capture.md)'s and
[`architecture/memory-writer.md`](memory-writer.md)'s.

## Extension points

**Another maintained ledger** (a reader served at `family`, say) joins `memory.ledgers`'
on-demand clearances; the writer, the view, the note and the tools need no change.

**A new channel** needs an explicit clearance entry, or it reads at the least-sensitive tier
by default. Write access is a separate, deliberate addition
([`architecture/memory-capture.md`](memory-capture.md)), and the retention list is a third
([`architecture/memory-lifecycle.md`](memory-lifecycle.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/ledgers.py`
- `casa/rootfs/opt/casa/sensitivity.py::clearances_served`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_channel`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_origin`
- `casa/rootfs/opt/casa/channel_trust.py`
- `casa/rootfs/opt/casa/tools.py::_origin_clearance_markers`
- `casa/rootfs/opt/casa/tools.py::_engagement_rebuild_fence`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.lower_origin_clearance`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.clear_context_rebuild_pending`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.handle_update`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._resume_and_ready`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.start`

**Tests**
- `tests/test_sensitivity.py::test_readable_tiers_is_clearance_and_below`
- `tests/test_sensitivity.py::test_clearance_docstring_states_the_fail_closed_direction`
- `tests/test_butler_friends_view.py`
- `tests/test_memory_withdrawal.py`

**Related**
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/memory-writer.md`](../architecture/memory-writer.md)
- [`architecture/memory-serving.md`](../architecture/memory-serving.md)
- [`architecture/memory-tools.md`](../architecture/memory-tools.md)
<!-- END SOURCEMAP -->
