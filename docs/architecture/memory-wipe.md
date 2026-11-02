---
last_reviewed: 2026-11-02
---

# Memory wipe: consent, the restart, and the container-start step that erases the registry

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The operator-consented erasure of memory: the three doors that can ask for it and the
consent each demands, the intent they record and the app restart they request, the
container-start step that carries the wipe out, and what `casa-main` does when it finds an
intent at boot. What a wipe erases is exactly the registry of
[`memory-store.md`](memory-store.md); the transcripts and pointers it resets are described
with their writers ([`memory-capture.md`](memory-capture.md),
[`memory-lifecycle.md`](memory-lifecycle.md)).

## Mental model

**A wipe is a whole, memory-only erasure, run at container start.** Consent records an
intent and asks the Supervisor to restart the app; the wipe itself runs in
`init-memory-maintenance`, an s6-rc oneshot that starts before every service that can read
or write memory. `casa-main` never runs it. It erases every row of the registry: `memory.db`
whole, the capture spool, the search index, local exports, legacy transcripts, every SDK
transcript of every role and engagement, the session pointers, the explanation records and
each engagement's executor-memory block. Reminders, triggers, files, settings and prompts,
pending tasks, plugin data and logs stay; a kept item can still mention something erased.

**Three doors, one consent posture.** The terminal door (`casactl memory-wipe --yes` →
`POST /admin/memory/wipe`) is root-gated by the peer-credential check every admin route
has, plus an explicit confirm field. The agent door (`wipe_memory`) posts an Approve/Cancel
keyboard carrying the confirmation text — what is erased, what stays, that Casa restarts,
and that copies outside Casa are untouched — to the configured operator, and acts only on
the operator's own tap, from the broker's finish hook outside any turn. With no operator
configured nobody can consent, and the tool door refuses everyone.

**Which door a shipped install actually has.** The terminal door, and only that one. A Casa
tool reaches an agent's MCP server only when that agent's resolved grants name it, and no
shipped runtime, role or executor artifact names the wipe tool — so the agent door exists,
is correct, and is held by nobody. An operator's own agent configuration can grant it, and
the tool's gates then apply unchanged. Because the door is unreachable by default, every
shipped resident's doctrine carries the matching refusal: claim the capability only when
the tool is actually present, and otherwise say plainly that this agent cannot do it and
name the terminal command.

**On consent** (`memory.maintenance.request_wipe`) a door writes
`/data/memory/maintenance.json` — kind `wipe`, the request time, an empty list of attempts
and of notified container starts — by atomic write with fsync of the file and its
directory, replies "Forgetting now. I'll restart and be back in a minute.", and asks the
Supervisor for the app restart at once (`supervisor_restart.request_app_restart`, the same
`POST /addons/self/restart` the configurator's restart uses). The tool door first stops the
Telegram updater: in polling mode that makes one last `getUpdates` with the next offset,
which confirms the consent tap, so the tap is never delivered again after the restart. A
webhook update is acknowledged when it is queued, before any handler runs. A second wipe
while one is pending answers that it is already scheduled and asks for the restart again;
a pending restore intent is never replaced by a wipe.

**The window before the restart has no admission guarantee.** Nothing is refused, drained
or fenced: turns, reminders, triggers, jobs, delegations and engagements run as usual, and
whatever they write to memory is erased at the next container start. One courtesy check
remains: once the Supervisor has accepted the request, an operator message on the DM path
or in an engagement topic gets "Restarting to wipe memory — send it again in a minute."
and no turn, for at most five minutes. The flag is in-process and set only by a successful
answer. If the request fails, or five minutes pass with no restart, the operator gets one
line ("I couldn't restart myself, so memory isn't wiped yet. …"), update intake resumes,
and Casa keeps running; the intent stays for the next container start.

**The container-start step** (`memory.maintenance.run`, `python -m memory.maintenance` from
`scripts/memory-maintenance.sh`). `init-memory-maintenance` depends on `base`, and
`init-setup-configs` depends on it. Every longrun reaches `init-setup-configs`, and every
engagement service is written with that single dependency, so all of them start after the
step. With no intent it only removes a staged restore source that has no intent. With one,
before its first deletion it appends this container start's attempt — a hash of the host's
boot id and PID 1's start time — and writes the intent, so an attempt that is cut off is
still seen as one. (A test install can make it sleep at this point, before its first
deletion, through `CASA_TEST_MAINTENANCE_SLEEP_S`, to measure a long step against the
stage-2 limits.) Then the wipe loop:

- **A. Retention.** A workspace whose recorded status is not `UNDERGOING` and whose
  `retention_until` has passed is deleted whole, with its control directory and its uid's
  outbox, by the workspace sweep's own rule.
- **B. Locate.** Every remaining workspace `CLAUDE.md` is classified. If any
  executor-memory section cannot be located, the step stops before deleting anything.
- **C. The registry.** Each row's wipe rule is applied in turn: files and directories
  deleted whole, every transcript root emptied, the session pointers reset to `{}`, each
  producer's declared staging files deleted, every executor-memory section blanked and
  every cache emptied. A row whose rule the loop does not know fails the attempt.

A fresh, empty store is then created with `meta` recording the first boot as complete and
the outcome, and the intent is removed last. Every deletion treats an absent path as
already done, so a run cut off anywhere re-runs to the same result. Any error stops the
run and is recorded in the attempt; the script always exits 0, so a failure never stops the
container from starting. Once the channels are up, `casa-main` sends "Done. I've forgotten
everything. Your reminders, triggers, files and settings are still here." once, claimed in
the same transaction that marks it sent.

**The executor-memory section keeps every other byte.** From this release
`provision_workspace` writes the cache before any `CLAUDE.md`, writes `CLAUDE.md`
atomically, and both it and `refresh_claude_md` wrap the block between two fixed markers
with every marker string removed from the block first, so memory text cannot forge one.
Every other input — the template text, the task, the context — has any marker string
neutralised (its `<` written as `&lt;`), and the template is filled in one pass, so a value
is never scanned for placeholders again: the one marker pair comes only from the slot. A
template with no slot caches nothing. The step classifies each file:

1. **Markers** — exactly one pair, in order: the content between them is emptied.
2. **A legacy block found through its cache** — no markers, the non-empty cached text
   occurs exactly once, and it sits exactly between the template's own text on each side
   of its slot (up to the neighbouring placeholder, or the template's edge), read from the
   executor type the engagement recorded. A cache cut off mid-write still occurs once, as a
   prefix, but the template's text does not follow it. The block is replaced by an empty
   marked section, so a re-run after a crash finds case 1.
3. **An empty legacy block** — no markers, an empty cache, and no
   `## Prior engagements (lessons learned)` line, which opens every non-empty block the
   pre-release producer wrote: nothing to blank.
4. **Not locatable** — anything else: more than one marker pair, a cached text whose
   boundaries the template does not confirm (a truncated cache, a template edited since, an
   executor type or template that is gone), or an empty cache beside a file that holds
   that line. The attempt fails and names the engagement.

A cache is emptied only after its file is blank. Every cache is also enumerated on its
own: one with no `CLAUDE.md` beside it is an orphan, left by a sweep or a provisioning cut
off between its two writes, and is emptied at once. Rewrites never follow a symlink and
keep the file's owner and mode; nothing below an engagement's directory is followed.

**A pending intent at `casa-main` boot.** Right after option and secret resolution — before
plugin environment, memory construction, boot replay, the writer, the scheduler, the
channels, the internal socket and the public app — `casa_core.main()` reads the intent:

- **No attempt recorded**: no step has reached a deletion, so memory is untouched. Casa
  boots normally and, once its channels are up, asks for the restart again.
- **Any attempt recorded**, this container's or an earlier one's: memory may be partly
  deleted. Casa enters notification-only mode: it records this container start in the
  intent, then makes one raw Bot API `sendMessage` to the operator chat — the latest
  attempt's error ("The memory wipe failed at startup (<reason>). I'm not taking messages
  until it succeeds; restart the app to retry."), or, for an attempt cut off before it could
  record one, "The memory wipe was interrupted before it finished; restart the app to
  retry." — and idles until it is stopped. It requests no restart, so it cannot loop.

**`setup-configs.sh` waits for the wipe.** While `maintenance.json` exists it skips the
`/root/.claude/projects` migration — both the copy into the resident home and the symlink —
so a real directory stays where it is for the next step to delete. No other oneshot, nor
any program one runs, touches a registry path's content.

**After the restart.** Session pointers are empty, so every conversation starts fresh.
An open claude_code engagement keeps its record and workspace; boot replay finds that its
session has no transcript left and drops the control-directory session pointer, so its run
script starts a fresh session (`drivers.workspace.drop_session_without_transcript`).

`memory_wipe.py` keeps the retain fence that long-term bank writers pass until the bank is
removed. Its former live orchestrator is no longer called by any door, and the bank itself
is not touched by this wipe.

## Contracts & invariants

**INV-MEM-035**: The wipe loop runs only in the container-start step, which every longrun and every engagement service starts after; a later oneshot's content operation on a registry path sits behind the `maintenance.json` check.

Pinned by a scan of every caller of the loop and of the step, by the real s6-rc graph (the
step is a oneshot in the `user` bundle, `init-setup-configs` depends on it, every longrun
and the engagement services `s6_rc.write_service_dir` writes reach it), and by a scan of
every oneshot script and the programs it runs, in which the projects migration's copy,
removal and link are the only content operations on a registry path and all three sit
inside the guard. Creating an empty directory and the `{}` of a missing `sessions.json`
are exempt by name: they carry no memory.

**INV-MEM-036**: The step records this container start's attempt in the intent before its first deletion; with any attempt recorded `casa-main` opens no memory and starts nothing, records the container start before its one line and requests no restart; with no attempt recorded it boots normally and requests the restart again.

What it does not cover: the line is a single attempt, and a crash between recording the
container start and sending costs that start's line rather than sending a second. The
facts the order rests on — a Supervisor app restart ends every process of the container,
`/run` starts empty, and a respawn or a runtime `s6-rc-update` does not re-run the step —
are measured on a test install, not here.

**INV-MEM-038**: The wipe executes only on consent at one of its doors, and erases exactly the registry: every row's path and declared staging pattern ends empty or reset, and every byte of a workspace `CLAUDE.md` outside its executor-memory section is kept.

Pinned by canaries in every row — the store's tables, the spool and its drain snapshot, an
in-progress export, legacy transcripts with an unfinished backfill record, a transcript of
every role and of an engagement, the session pointers, an explanation record, the
executor-memory block of an open and of a retained completed engagement, and a staging
file each producer's atomic helper leaves when a real process is killed before its rename —
plus one captured after consent, all at 0 bytes after the step, with kept items
byte-identical. What it does not cover: a kept item may still mention something erased, and
copies outside Casa (a Home Assistant backup, a downloaded export) are untouched. A
transcript root an engagement replaced with a symlink is not followed.

## Failure behavior

**A section cannot be located.** The step stops before any deletion (or, if a file changed
since the locate pass, at that file), records "executor memory of engagement <id> could not
be located", and keeps the intent; Casa starts in notification-only mode and names the
engagement. Deleting that workspace, or restarting once its retention has passed, lets the
next attempt succeed.

**The step is cut off.** A kill anywhere leaves the intent with this container's attempt
and no error; Casa reports the interrupted line, and the next container start runs the
step again to the same result.

**The intent file cannot be read.** The step deletes nothing and rewrites it as an attempt
whose error says the request could not be read; every later start does the same. A
restart cannot fix that, so Casa's one line says so and names the action that does:
"Memory maintenance could not read its record, so nothing was deleted. Remove
/data/memory/maintenance.json, then restart the app."

**The restart request fails or never happens.** One line to the operator, Casa keeps
running normally with no courtesy line, and the intent waits for the next container start.

## Extension points

**A new door** that can ask for a wipe carries its own consent and calls
`memory.maintenance.request_wipe`; it never runs the loop.

**A new place memory lives** joins the registry with a wipe rule the loop knows
([`memory-store.md`](memory-store.md), INV-MEM-025); a row whose rule has no handler fails
the attempt rather than being skipped. **A new oneshot** that touches a registry path's
content does so inside the `maintenance.json` guard, or the scan fails.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/maintenance.py`
- `casa/rootfs/opt/casa/memory/executor_block.py`
- `casa/rootfs/opt/casa/supervisor_restart.py`
- `casa/rootfs/opt/casa/memory_wipe.py::RetainFence`
- `casa/rootfs/opt/casa/tools.py::wipe_memory`
- `casa/rootfs/opt/casa/internal_handlers.py::build_admin_memory_wipe_handler`
- `casa/rootfs/opt/casa/drivers/workspace.py::drop_session_without_transcript`
- `casa/rootfs/etc/s6-overlay/scripts/memory-maintenance.sh`

**Tests**
- `tests/test_memory_wipe.py`
- `tests/test_memory_wipe_crash.py`
- `tests/test_executor_memory_blank.py`
- `tests/test_memory_pending_intent.py`
- `tests/test_memory_maintenance_only_in_oneshot.py`
- `tests/test_s6_memory_maintenance_graph.py`
- `tests/test_wipe_door.py`
- `tests/test_wipe_memory_tool.py`
- `tests/test_admin_memory_wipe_route.py`
- `tests/test_retain_fence.py`
- `tests/test_retain_fence_writers.py`

**Related**
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/engagement-workspace-access.md`](../architecture/engagement-workspace-access.md)
<!-- END SOURCEMAP -->
