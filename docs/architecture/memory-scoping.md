---
last_reviewed: 2026-11-09
---

# Memory scoping

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Which stored facts a given reader gets back: the read-clearance model — per channel, per
Telegram sender via the stamped origin, failing closed — the ledgers Casa maintains, one per
clearance it serves, each written only from what that clearance can read, the check every
read path makes on each object it returns, the clearance an engagement
inherits from its creating turn, and who may steer a job. How a
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
stamped with, and that is what its reads resolve against: its own recall and the engager-side
context it can query. No engagement starts another: an engagement is created only by
starting a job, through the `start_job` tool or a scheduled job's fire
(`tools.py::start_scheduled_job`), and no engagement session can call `start_job` — a
specialist engagement's casa-framework tools stay inside the consumer-safe ceiling
(`specialist_component.SPECIALIST_CASA_TOOL_ALLOWLIST`, checked again at dispatch), and a
resident-hosted plugin job holds only `report_job_progress` and `emit_completion`. Every
engagement is therefore created by a resident's turn, a specialist's delegated turn (whose
origin copies its caller's route and clearance), or a scheduled fire (which stamps them as the DM ingress does), and its record
carries that turn's markers. An engagement
whose record predates this (or came from an origin that stamps no
route) keeps the channel-keyed behaviour.

Inheritance at creation is only half the rule, because a job is *steerable*. It is steered
only in the direct chat: a reply quoting one of its posts goes into it when the sender is the
authenticated operator who started it, in that chat (INV-BGJOB-012,
[`background-job-direct-chat.md`](background-job-direct-chat.md)), and a tap on its buttons is
resolved for that operator alone. Nothing written in the agent-log group reaches a job
(INV-TG-016, [`agent-logs.md`](agent-logs.md)). So the job keeps reading at the clearance of
the turn that started it, and its session, which holds what it read there, takes direction
only from the operator who started it.

What this deliberately does not do: the agent log, when one is set up, is readable by every
member of its group, so what a job's agent writes there is a disclosure the group carries
regardless; the operator decides who is in it.

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

**INV-MEM-045**: An engagement's reads resolve clearance from the origin markers its own record carries.

Enforced where the markers are resolved for an access-control decision: the ambient origin
wins when it has a route of its own (an in-process delegated turn), otherwise the bound
engagement record supplies them. Every read an engagement can perform resolves through it:
its own recall and the engager-side context.

What it does not cover: a record carrying no markers falls back to channel-keyed clearance,
which on Telegram is private — so this tightens what a *newly* stamped origin can reach, and
does not reach engagements created before the markers existed.

## Failure behavior

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
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.handle_update`

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
