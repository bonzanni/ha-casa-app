---
last_reviewed: 2026-11-02
---

# Serving memory: the session's view and the correction note

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How what the writer filed reaches an agent: the view of a ledger a session is served at its
start — the whole ledger while it fits the role's budget, its index past it — where that view
sits in a prompt, how it stays frozen for the session, and the correction note that rides on
every turn of every memory role. How events become a ledger is
[`architecture/memory-writer.md`](memory-writer.md); the store and its registry are
[`architecture/memory-store.md`](memory-store.md); the memory tools and what a caller
may claim from their results are [`architecture/memory-tools.md`](memory-tools.md); who may read what —
read clearance per channel and sender — is [`architecture/memory-scoping.md`](memory-scoping.md),
which also owns the executor archive.

## Mental model

**A session reads one ledger, chosen by its clearance.** A role whose `memory.token_budget`
is above 0 is a *memory role*; its budget is the size of the view it is served, in tokens
(the assistant's is 50 000, the butler's 10 000; 0 serves nothing). A session reads the
ledger of the highest maintained clearance at or below its own: `private` always, `friends`
while a reader at `friends` is served a view — the butler, on voice — so a voice session is
served the `friends` ledger and its note, and a stranger on Telegram (`public`) is served no
view and no note. Which ledgers are maintained is
[`architecture/memory-scoping.md`](memory-scoping.md)'s. A desk turn is the operator's own and
reads at `private`.

**The reader is part of the session.** A session's prompt is written for one reader: its
view, its channel context. So the turn's read clearance joins the structural surface digest a
session is resumed on, and a turn read at another clearance than its chat's session never
reuses or resumes that client: it starts a session at its own clearance. `private` adds
nothing to the digest, so the operator's sessions are unaffected; in a chat whose turns
alternate between the operator and another sender, each switch starts a new session.

**The view is built locally, at session start, with no model call.** On a fresh connect, and
on a cold reconnect of a resumed session, the view is rendered in one read transaction, and
the snapshot's `commit_no` becomes the session's *base*. It is the whole ledger while it
fits: every current entry in full, current value first, grouped under its topic heading. A
warm turn keeps the client its session was built with, so it reads nothing.

**Past the budget, the view is an index, built bottom-up.** Each level is added only when the
level below no longer fits: entry lines (an entry's need line and cues, dated, never its
value), then group lines (an entry under a group folds into its group's line, with the
group's date range and entry count), then topic lines. The finest level that fits is the
start. From there the cheapest refinement that still fits is applied, again and again: a
topic line opened into its members' lines, a group line into its entries' lines, an entry
line into the full entry. Nothing is pinned and nothing is ranked by age. If even the
coarsest level is over the budget it is served anyway, because an invisible entry is worse
than a long prompt, and the view says so. The meter flags the overflow, and the nightly pass
queues a regroup for that ledger. Every line of a view comes from one ledger, so every line
carries that ledger's tier.

**The view is frozen for its SDK session.** The chat's row in `session_views` records the
ledger, the base, the clearance and budget it was rendered for, the renderer's version and
the hash of the block — no memory text — and, once the turn that served it has published its
session, the id of that SDK session: the record belongs to that one session. A commit after
the base never changes what the session was served. Every rendering goes through one function
that rebuilds the ledger *as of* a commit from `revisions` (each revision row holds an
object's whole state), so a reconnecting session renders its view again from its base and
checks the hash. A session is used again — resumed, or its warm client reused — only on a
record bound to its own id, made for the same ledger, clearance, budget and renderer; a
resume also re-renders the view and checks its hash. The rule is checked when a session
connects and again at the end of every turn, inside the turn's session gate: a session
without such a record (its view could not be recorded or bound, or its record was replaced
by another session's) has its warm client released then, and any session that cannot resume
on its record starts afresh, once, and the reason is logged. The one exception is a store
that cannot be read at all: that is no verdict, and the session is kept on its own frozen
view. The session's resume pointer is dropped durably before the new view is rendered and
recorded, so a retry of that turn (a connect that times out) starts a session too and never
resumes the old one on the new view. The
turn's explanation record names the ledger and the base.

**The view has one slot in the prompt.** For a resident the compiled projection is fixed;
the view is placed in its one opaque slot, after `<persona>` and before `<role_doctrine>`,
outside the projection's admission ceilings and outside its digest, so a memory commit never
changes the digests a session's resume is gated on. A specialist or executor with a
composed prompt gets the block at the head of its prompt. A delegated specialist runs one
turn: its view, at the delegating turn's clearance and within the specialist's own budget,
goes into its user prompt beside the brief, read in the same snapshot as its correction
note, and nothing is recorded for it. When the store cannot be read, a delegated specialist
is told that memory could not be checked, so it never reports that Casa lacks information.

**Corrections reach a frozen session through the note.** A correction can arrive in another
conversation at any time, and a session's view is frozen. So every turn of a memory role
carries, read in that turn's own snapshot, every correction its view does not reflect: each
one pending in the session's ledger (still waiting for the writer; `deferred` counts as
pending), and each one committed after the session's base. The note gives the operator's
own words, verbatim, with the local time he said them. It rides in the turn's Casa notes
block, which is stripped from what is captured, on every turn — fresh, warm, after a
reconnect or a resume, on any channel — so neither compaction nor a reconnect can drop it,
and nothing records that a correction was "shown". A delegated specialist's note is in its
prompt with its view. An in-casa engagement of a memory-role specialist is served no view,
and its base is derived, never recorded, and deliberately conservative: the newest commit at
or before five minutes before the engagement's own start, found by comparing its record's
creation time with the times the writer stamps on every commit's revisions and dispositions.
A commit stamps its time before it lands, and the two times come from a wall clock; the margin
absorbs both, so a correction committed close to the start is counted as after it. No
failure at any turn can move the base, and every engagement turn carries the corrections
pending and those committed since the engagement started, ahead of its message. Since an
engagement shows no view text, the note may repeat a correction it already reflects; it
never leaves one out. A resident session with no record bound to it is
owed the pending corrections only, until its next reconnect starts it afresh. A turn that starts a session reads its note first, and its view is rendered as of that
note's own snapshot, so the two describe one state of the store: a correction filed while
the session starts is after its base, and owed to its next turn. On that first turn the note
may also name a correction the view already reflects; that is redundant, never wrong.

**The doctrine says how to use what is served.** The view opens with a fixed head saying
what it is: the operator's own record, dated, current value first, an earlier value noted
with the date it held until. In index mode the head also says that a line starting with an
id names what an entry or group holds, never its value, that `open_memory` returns it, and
to open an entry whenever its need might touch the turn. The assistant's doctrine adds:
his health, money and family are his to bring up; a current value in memory is used without
asking him to confirm it, and his own correction in the message wins at once; and a day's
saved conversation can be read in order with `browse_memory_day`.

## Contracts & invariants

**INV-MEM-030**: Every turn of a memory role whose clearance reads a ledger carries, read in that turn's own snapshot, every correction pending in that ledger and readable at that clearance, and every correction committed there after its session's base, as the operator's own dated words — on a fresh, warm, reconnected or resumed turn, on any channel, in a delegated specialist's prompt and on an in-casa engagement turn of a memory-role specialist.

Enforced by `memory.note`, which reads the session's row and both kinds of correction in one
transaction through the pending view (an engagement's base derived from its start), and
by the four places that put the note on a
turn: the resident turn's notes block, built for every channel before the session's client
is chosen; the delegated specialist's prompt, read with its view in one snapshot; and the
in-casa engagement turn, read before the delivery fence and put on the prompt without
awaiting.

What it does not cover: a resident session with no record bound to it gets the pending
corrections only, since no base says which committed ones it lacks; a turn whose store is
unavailable carries no note, and logs that it could not read one.

## Failure behavior

**The store is unavailable or fails while a view is served.** The failure is logged. A
resumed session keeps resuming, on its own frozen view: the CLI pinned its system prompt, the
view included, when the session was created, so it keeps exactly what it was served and
keeps its conversation through the outage. A new session starts with no memory block and no
record bound to it, so at the end of the first turn after the store can be read again its
warm client is released, and its next turn starts afresh on a view. A delegated specialist is told
memory could not be checked.

**The store is unavailable or fails while the note is read.** The turn runs without the note
and the failure is logged. The note comes back on the next turn the store answers.

**A view's record cannot be written or bound** (another connection holds the store's write
lock past 50 ms, the same bound capture waits). A view that could not be recorded is not
served, and the turn runs at once without one; one recorded but not bound to its session was
served. Either way no record is bound to the session, so its warm client is released after
the turn and its next turn starts afresh on a current view.

**The ledger is too large for the coarsest index level.** The view is served over budget, with
a line saying every entry is still reachable; the meter reports the overflow and the nightly
pass queues a regroup.

**The renderer changes.** Its version is part of each record, so every session recorded by an
earlier renderer starts afresh at its next reconnect rather than being served a view nobody
can show it was given.

## Extension points

**A new served role** sets `memory.token_budget` above 0, which makes it a memory role on every
path above. Its sessions read the ledger their clearance reads.

**Another maintained ledger** (a `family` one, say) is one more entry in the maintained
ledgers ([`architecture/memory-scoping.md`](memory-scoping.md)): sessions at that clearance
are then served its view and its note, with no change here.

**A change to the rendering** bumps the renderer version, so recorded sessions start afresh
instead of failing their hash check one by one.

**A new path that puts memory in a prompt** must serve the note with the view, from the same
snapshot, and add itself to INV-MEM-030's tests.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/view.py`
- `casa/rootfs/opt/casa/memory/note.py`
- `casa/rootfs/opt/casa/memory/sessions.py`
- `casa/rootfs/opt/casa/prompt_compiler.py::projection_for`
- `casa/rootfs/opt/casa/agent.py::Agent._serve_memory_view`
- `casa/rootfs/opt/casa/agent.py::Agent._memory_note`
- `casa/rootfs/opt/casa/tools.py::_delegated_memory_block`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::_engagement_memory_note`

**Tests**
- `tests/test_memory_view.py`
- `tests/test_memory_frozen_view.py`
- `tests/test_memory_note.py`
- `tests/test_memory_view_bench.py`

**Related**
- [`architecture/memory-writer.md`](../architecture/memory-writer.md)
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
