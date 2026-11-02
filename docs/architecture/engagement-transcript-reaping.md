---
last_reviewed: 2026-11-02
---

# Engagement transcript reaping: deleting a finished engagement's CLI transcripts

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where a finished engagement's CLI transcripts are deleted: the pass that removes the files a
terminal `in_casa` record attributes to it once their retention has passed, the gate it and the
`claude_code` workspace sweep ask first, what it never selects, and what it retries.
How a durable engagement ends — the terminal transition whose on-disk status this pass waits
for — is [`architecture/engagement-finalization.md`](engagement-finalization.md).

## Mental model

**The terminal record is the unit, never the folder.** The pass deletes what a terminal
record names or owns and nothing else: it never lists a folder, so what no record names stays
on disk. **A transcript outlives its engagement by the retention, and never outlives an
unconfirmed run.** The invariant below states what that selects, and its gaps.

## Contracts & invariants

**INV-ENG-027**: Once an `in_casa` engagement's terminal status is on disk and `TRANSCRIPT_RETENTION_DAYS` have passed since its `completed_at`, a pass run at scheduler start and every six hours deletes its CLI transcripts — a plugin job's whole per-engagement project folder, or, for a specialist or executor, each session the record names (its current session, every session its background job lists, and every session a clearance downgrade retired from it) in that record's own project folder — each only where the run-confirmation gate passes, and a plugin job's working folder `/data/engagements/<id>` at once, and selects nothing else; a `claude_code` workspace past its `retention_until` is deleted whole only where the gate passes for it, and is kept whole otherwise. A pass that cannot import the SDK's folder lookup deletes no transcript.

**The gate** is `memory/transcript_runs.py`'s, asked at each delete and never copied: a
session is kept while an unconfirmed run names it or an unconfirmed run in its folder names
no session, and a whole folder (a plugin job's, a workspace) is kept while any unconfirmed
run names it. The finalize registers its summary run before the terminal flip that makes the
record eligible here (`tools.py::_register_summary_run`): a row naming only the folder plus
one per session the record already names, confirmed together. A crash between the flip and
the summaries' capture therefore keeps every transcript in that folder — a sibling
engagement's in a shared folder too, and a session published after the registration —
until a wipe, counted unresolved (INV-MEM-031, in
[`architecture/memory-lifecycle.md`](memory-lifecycle.md)). After a delete the rows that
named it are removed. The pass reads no transcript.

Casa owns transcript deletion: the CLI's own cleanup never fires for an `in_casa` launch, since
no SDK launch loads user settings or passes `cleanupPeriodDays` (INV-MEM-021, in
[`architecture/memory-lifecycle.md`](memory-lifecycle.md)), and the resident time-to-live sweep
([`architecture/memory-lifecycle.md`](memory-lifecycle.md)) only ever reaps resident
sessions. An `in_casa` session writes `<session>.jsonl` and a `<session>/` folder (its
subagents and oversized tool results) under the CLI projects root, in a folder named after
the session's working directory; a `claude_code` engagement keeps its transcript inside its
workspace, which goes with the workspace. Nothing resumes or reads an `in_casa` transcript
once its record is terminal, so the pass deletes the files the record attributes to it. The
folder comes from the launch's own working-directory constructor
(`engagement_transcript_reaper.py::transcript_cwd`, the same one a run registers under) run
through the SDK's lookup — a plugin job's folder is unique to the engagement, so it goes whole with every
batch's session in it; a specialist's folder (its configured working directory, else its
agent home) and the executors' shared `/config` folder hold other sessions, so only the
named ones go, a zero-byte file included. A clearance downgrade (INV-MEM-011) records the
session it evicts on the record in the clamp's own write, so a restart or the rebuild's new
session cannot overwrite the only pointer to it first. A plugin job's working folder needs no
SDK lookup: it goes even when the lookup is missing or the project folder is already gone,
and an entry an operator's delete removes mid-walk is skipped, not an error.

The condition is the status *on disk*, not in memory: a strict terminal transition whose
write fails is rolled back to live, and a non-strict one whose write fails leaves the disk
saying live — either way a restart would resume the session. Such a record waits for a
later write. Every pass re-visits every terminal record still loaded, with no "done"
marker, so a session named after a pass, or a removal that failed, is handled by the next
pass; one record's failure never stops another's.

The folder lookup is the SDK's own private helper, imported when a pass starts and never
when the module loads: Casa imports the pass before scheduling it, so an import at load
would stop Casa booting on any SDK version that lacks the helper. A pass that cannot import
it logs one warning naming the helper, counts an error, deletes no transcript and tries again on
the next pass. The end-to-end test image's mock SDK carries a copy of the same lookup, so the
pass runs there as it does in production.

What it does not cover, and these are gaps rather than retention: it never lists a folder,
so sessions no record names stay — any plugin-job folder whose record is gone, and the
sessions of a specialist that has since been uninstalled (its folder can no longer be
derived) or given another working directory. (A delegation's session is deleted by the
delegated retention pass and a utility one-shot writes none: INV-ENG-028, in
[`architecture/delegation.md`](delegation.md).) A terminal record's row stays in the
tombstone for the retention plus a two-day margin (`_terminal_retention_days`), and the pass
never selects a record without one, so a removal that fails for that long, or a transcript
an unconfirmed run kept, is not retried; it stays until a wipe.

## Failure behavior

**A transcript removal fails.** It is logged and counted, the pass moves on to the next
record, and the next pass retries it. A tombstone the pass cannot read selects nothing that
pass, and warns only when a terminal record waits: on an install that has never run an
engagement the file was never written, which is not a fault.

## Extension points

**A new place an `in_casa` launch writes a session** has to be named by the record, or by a
folder the record alone owns, for the pass to delete it: the pass selects only what a
terminal record attributes to it, so a session no record names stays on disk. **A new
deleter of engagement transcripts** asks `transcript_runs.blocked` at its delete.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_transcript_reaper.py::reap_engagement_transcripts`
- `casa/rootfs/opt/casa/engagement_transcript_reaper.py::transcript_cwd`

**Tests**
- `tests/test_engagement_transcript_reaper.py`
- `tests/test_engagement_transcript_reaper_pins.py`
- `tests/test_transcript_confirmation.py`
- `tests/test_transcript_retention.py`

**Related**
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/background-job-fresh-sessions.md`](../architecture/background-job-fresh-sessions.md)
<!-- END SOURCEMAP -->
