---
last_reviewed: 2026-11-02
---

# Memory tools: search, browse, open and correct

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The four tools a memory role reads and corrects its memory with — `search_memory`,
`browse_memory_day`, `open_memory` and `correct_memory` — the search index behind the first,
and the engager query's read of an engager's memory. What each returns, at which clearance,
and what a caller may claim from it. The view of a ledger a session is served at its start,
and the correction note, are [`memory-serving.md`](memory-serving.md); how clearance is
resolved per channel, sender and engagement is [`memory-scoping.md`](memory-scoping.md); what
a correction mark does to the pending predicate and the writer is
[`memory-writer.md`](memory-writer.md); the store and its registry are
[`memory-store.md`](memory-store.md).

## Mental model

**Two things can be read, and each has its tools.** The *ledger* is what the writer filed:
a session is served it at its start, whole or as an index whose lines carry ids, and
`open_memory` opens entries by those ids. The *saved conversation* is the verbatim store,
`events`: `search_memory` finds events by their words, best match first, and
`browse_memory_day` reads one day, or up to seven, in time order. No turn reads either on
its own; a model calls a tool.

**Every object a tool returns is checked against the reader's clearance, one by one.** The
clearance is the one the turn path uses: the turn's stamped origin, or for an engagement's
call the markers its own record carries (INV-MEM-008). A search hit and each of its window
neighbours are kept only when that clearance reads the event's tier, read from the store at
the time of the search; a browse filters every event of its days the same way. Both filter
**before** anything is paged or counted, so a page break, a page count or an event count
never moves because of an event the reader cannot read. `open_memory` reads only the ledger
the reader's clearance is served (the highest maintained ledger at or below it): an id of
another ledger gets exactly the answer an id that exists nowhere gets, `unknown`. A
clearance lowered while a read runs — an engagement's clamp — is noticed when it returns,
and the read runs again at the new clearance before anything is returned; for a call bound
to an engagement, that engagement's own record, read live, is the authority, not the
origin its call inherited.

**Which specialist's output is kept is decided when it is captured.** Output a specialist
produces while it writes no memory is never stored (INV-MEM-032), so no tool can return it;
what it said while it was a memory-writing specialist was stored then, and stays readable
until a wipe.

**The search index is derived, and only ranks.** `search.db` sits beside the store. It holds
one FTS5 table with each event's text under its `seq`, its frozen local day, and the highest
`seq` it has indexed. `events` only grows — a `seq` is assigned inside the store's single
write transaction and an event's text never changes — so before each search the index
catches up by `seq`, under its own write lock. At boot a missing or unreadable index is
rebuilt from the store with no model call. It is never exported or restored; the wipe
deletes it, and the next boot builds it again. The index holds no tier, because
classification can raise one after indexing, and it is never what a hit shows: each
candidate it ranks is read back from `events` in the search's own read snapshot. An index
left behind by a store it no longer matches could therefore rank wrongly, but could never
show text the store does not hold. The query is split into its words mechanically, and an
event matching any of them ranks by BM25.

**A hit carries what is needed to place it.** Its date, weekday, time and speaker — the
reader's own earlier words read as `you` — its text (cut past a length, with a pointer to
the day's browse), whether Casa's schedule fired the turn, and up to two readable lines on
either side from the same conversation that day. A page holds five hits; a page with more
behind it says so. A browse page holds about 10 000 characters, each event whole, each page
headed by its day.

**Days are the operator's.** `from` and `to` on a search, and `date` and `to_date` on a
browse, take a day or a named period — today, yesterday, this or last week (weeks start on
Monday), this or last month — resolved in the operator's timezone. A named period stands
for its first day as a lower bound and its last as an upper one; a browse of a named period
reads all of it. An event's day is the local day frozen when it was captured. A browse may
also take a time on its first day and one on its last, and reads the stretch between.

**Opened entries are current.** `open_memory` returns each object as it stands at its
latest committed revision: an entry in full, a group or topic as the lines of what it holds
(more ids to open), a retired one as retired. The session's view is frozen at its start, so
an object committed after that view is marked `changed_since_your_memory_above` — the
session's base read from its view record, and only when that record is bound to the SDK
session the call runs in. `with_history` adds each earlier version with the date it was
replaced and how.

**A correction marks his own words.** When the operator says something memory holds is
wrong, `correct_memory` marks the message he said it in — the operator event the turn
opened with. Capture stores that message before the model is called; once it is durable
its occurrence key is put on the turn's origin, and the tool finds the event by it. Only a
message already in the store is marked: one that had to spool is never drained on the
turn's path (a drain takes as long as its backlog and waits on spool writes), so the tool
asks for the background drain and answers `unavailable`, and a later call marks it once it
is stored. The mark goes through
`memory.corrections` (re-opening a filed event and queuing its correction job), and the
tool returns `queued` at once; a retried call changes nothing. The note carries his words
to every session from its next turn ([`memory-serving.md`](memory-serving.md)). The tool's
`what` argument names what he corrected for the model's own call; his message is what is
kept. A turn no message of his opened — a scheduled turn, an engagement, a delegation —
marks nothing, and says so.

**The engager query reads the same memory.** An executor's `query_engager` asks a
synthesizer that sees only the engager's view of the ledger its engagement's clearance
reads and the first page of a search for the question, read at that clearance. With
nothing readable, or when the synthesizer says `UNKNOWN`, the executor is told the
readable memory had nothing — not that nothing exists.

**Grants follow the roles.** The assistant holds all four tools. The butler, which serves
the voice channel where nothing is captured, holds the three reads. A specialist may be
granted the three reads (its ceiling holds no `correct_memory`). An untrusted webhook turn
gets `search_memory` alone, at its declared clearance.

## Contracts & invariants

**INV-MEM-010**: No memory read licenses a claim that Casa lacks something: every `search_memory` and `browse_memory_day` result, empty or not, carries one note, the same at every clearance, that what it returns is what is readable here and that finding nothing is not proof; a page with more behind it says so; a store that cannot be read is reported unavailable, never as an empty result; the engager query's synthesizer, the one reader handed an unframed slice, can only answer from it or return `UNKNOWN`, which reaches the executor with do-not-assert-absence guidance; and the executor lessons block carries the bounded-slice line.

Enforced in the search and browse tools, which attach `tools.MEMORY_READ_NOTE` to every
`ok` result and report an unavailable or failing store as `unavailable`; in the engager
query's unknown and unavailable arms; and in the executor archive, which puts
`tools.READABLE_SLICE_PROMPT_LINE` under its heading. The unavailable message
says when to tell: a person who asked is told memory could not be checked, and an outage
on a check nobody asked for is no reason to send anything (#1116).

What it does not cover: it is a statement about what a result *says*, not what a model does
with it. `open_memory`'s `unknown` is the answer for an id the reader's ledger does not hold,
and claims nothing about anything else. The served view is not a slice: it is the whole
ledger, or its index, at the session's clearance.

**INV-MEM-019**: A `search_memory` call given `from` or `to` returns only readable events whose frozen local day lies inside the days Casa resolved in the operator's timezone — a named period standing for its first day as `from` and its last as `to` — inclusive; the range and the reader's clearance filter every event before anything is paged; an empty in-range result carries the same bounded note as any other; a bound that cannot be read, or a `to` before `from`, is refused before the index or the store is touched; and a call without them reads every day.

Enforced in the search tool, which resolves each bound with `timekeeping.resolve_period` and
refuses before calling the search, and in `memory.search.search`, which filters the index on
the local day and the store on the tier before it pages.

What it does not cover: an event's day is when it was said, not when what it describes
happened.

## Failure behavior

**The store is unavailable or a read fails.** Each tool returns `unavailable` with the
message above; nothing is reported as empty. The engager query returns `unavailable` too.

**The index is missing or unreadable.** Boot rebuilds it from the store. A search rebuilds a
missing one itself. An index that cannot be opened during a run makes that search report
`unavailable` until the next boot rebuilds it.

**A lock is held.** Every connection a memory tool or the engager query opens — the store's
reads, the index's catch-up and its reads, the correction mark — waits for another
connection's lock no longer than the turn-path bound, `memory.view.BUSY_TIMEOUT_S` (50 ms),
whatever the store's journal mode. No memory tool drains the spool or takes its lock. Past
the bound the tool answers `unavailable` at once; a turn never waits on a writer, a drain or
another search's catch-up. Only boot's rebuild of the index waits the store's own, longer
bound.

**A correction cannot be marked.** No message of his in the turn: an error that says so,
nothing marked. His message still in the spool (the background drain is asked for), the
store down or busy: `unavailable`, worded so the model does not claim memory was updated — the
conversation itself still carries his words.

## Extension points

**A new memory read** opens every connection with the turn-path bound and joins the timing
pin in `tests/test_memory_tools_busy.py`, resolves clearance through `_memory_reader`, runs
its blocking read through `_read_memory` (off the event loop, re-run on a clamp), filters every returned
object by its own tier or ledger before paging or counting, and frames its `ok` result with
the shared note.

**A new derived file** gets a registry row whose producer the producer pin can run
([`memory-store.md`](memory-store.md)), and a rebuild in the boot check.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/search.py`
- `casa/rootfs/opt/casa/memory/browse.py`
- `casa/rootfs/opt/casa/memory/view.py::open_objects`
- `casa/rootfs/opt/casa/memory/corrections.py::mark_operator_event`
- `casa/rootfs/opt/casa/tools.py::search_memory`
- `casa/rootfs/opt/casa/tools.py::browse_memory_day`
- `casa/rootfs/opt/casa/tools.py::open_memory`
- `casa/rootfs/opt/casa/tools.py::correct_memory`
- `casa/rootfs/opt/casa/tools.py::_read_memory`
- `casa/rootfs/opt/casa/tools.py::_engager_context`
- `casa/rootfs/opt/casa/tools.py::query_engager`
- `casa/rootfs/opt/casa/timekeeping.py::resolve_period`

**Tests**
- `tests/test_memory_search_tool.py`
- `tests/test_memory_period.py`
- `tests/test_memory_browse.py`
- `tests/test_memory_open.py`
- `tests/test_correct_memory_tool.py`
- `tests/test_memory_tools_busy.py`
- `tests/test_query_engager_tiers.py`
- `tests/test_recall_readable_slice_framing.py`

**Related**
- [`architecture/memory-serving.md`](../architecture/memory-serving.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
- [`architecture/memory-writer.md`](../architecture/memory-writer.md)
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
<!-- END SOURCEMAP -->
