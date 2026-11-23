---
last_reviewed: 2026-11-23
---

# Triggers: who may write a resident's trigger file

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Who may write a resident's `triggers.yaml`: the guard that refuses an agent's file-tool write to it, the shell backstop and what it deliberately leaves open, and the one process lock every writer of the file serializes under. The triggers themselves — their types, registration, firing and one-shot cleanup — are [`triggers.md`](triggers.md); reminders, which live in the same file, are [`reminders.md`](reminders.md).

## Mental model

**A resident's trigger file has two writers, and only one of them is allowed to be an
agent's hand.** Reminders are ordinary entries in the operator's own `triggers.yaml`, so the
resident's reminder tools and the configurator's trigger edits target the same document. The
configurator runs in a separate CLI child process: it reads the file, decides, and writes
back — an interval that spans model thinking time, and one no lock may be held across. A
reminder set inside it was discarded by the stale rewrite, silently, and the commit that
followed staged the loss. So an agent does not write that file at all. The file tools are
refused for that path and the change is made *inside* Casa, in a read-modify-write held under
`trigger_write_lock.PASS_LOCK` so it cannot interleave with the reminder writer.

## Contracts & invariants

**INV-TRIG-011**: An agent's file-tool write whose path *resolves* to a resident's `triggers.yaml` is refused, and every writer of that file — the typed tools, the reminder tools, and the config reconciler's whole pass — serializes its read-modify-write under one process lock.

Two halves, and both are load-bearing. A PreToolUse guard — code-mandatory on every
resident and, since the resident-prompt guard's wiring made it visible, every *delegated*
resident too, and listed in the configurator's image-owned hook list — refuses the write. It is applied uniformly rather than only
to an agent the loader knows has `Bash`. No shipped resident carries `Bash` today (#460
removed it from the assistant, the last one that did), and the configurator does not either;
the guard's universality is what keeps whether it runs from depending on that fact. The typed replacement then makes the change *inside* Casa, and the read, judgement
and write are held under `trigger_write_lock.PASS_LOCK`, which the `config_sync` reconciler
also holds across its entire pass (#458): a reminder or configurator write can only land
before or after a pass, never in the middle of the reconciler's own read-decide-write, and
vice versa. The lock is what buys the serialization — earlier the tools bought it by refusing
to `await` and holding the event loop, but that left the reconciler, which runs on a worker
thread, uncoordinated; the shared lock covers all three writers and is taken off the loop via
`asyncio.to_thread`, so a held pass waits a worker thread rather than stalling the loop.

**The two tool families the guard covers are not equally decidable, and it asks each of them
a different question.** A `Write`/`Edit`/`MultiEdit`/`NotebookEdit` path is a literal:
resolved against the *session's* working directory — the configurator's is `/config`, a
resident's is its agent home, and assuming one of them is how a `../../` spelling read as
allowed — then normalized and symlink-resolved, and refused outright when no working
directory is reported at all, since a relative path is then not resolvable by anything.

That is exact for what it claims — a path that *resolves* to the file — and the wording is
load-bearing. A **hard link** to `triggers.yaml` is the same inode under a different name,
and `realpath` reports the alias, not the target; a symlink **retargeted between the check
and the write** is a race no pre-tool hook can close. Neither is decidable here, both belong
to the same family as the shell residual, and both are recorded in #460.

**The `Bash` half is a backstop and the invariant deliberately excludes it.** Four review
rounds by two independent models produced seventeen bypasses of it. Each round closed the
previous round's spelling and the next found another — a bare basename after a `cd`, quote
splices on operands and then on redirect targets, an opaque interpreter body, a `$PWD` only
bash expands, `command cp` and `bash -c "cp …"` past a list of write verbs, allowlisted
readers that turn out to write (`xxd` takes an output operand, `rg --pre=` runs an arbitrary
preprocessor). That is not a run of bugs. The two judgments it began with — *which* path a
shell writes to, and *whether* it writes — both range over open sets, so both were removed
in favour of predicates over closed ones: does the command name `triggers.yaml` at all, with
quoting stripped from the whole command first, and is it *provably read-only* — every
pipeline segment one of about twenty audited read programs named without a path (an agent can
write an executable called `cat`), no redirect, no substitution of any kind, with `git`
admitted by subcommand through the managed guard's own audited read-only set so an ordinary
`git diff` on an unrelated file still works. That closed sixteen.

What stays open is bash's own quoting, and no parser closes it: `tri$''ggers.yaml` and a
backslash-newline continuation name the file to the shell and something else to a tokenizer,
and ANSI-C quoting can encode any character. Nor is the allowlist truly closed while an agent
can put its own `cat` earlier in `PATH`. So this half catches the *accidental* form —
what a model following a stale recipe would actually type — and claims nothing more. The
real boundary for an agent with broad shell access is filesystem enforcement or not having a
shell over that tree (#460); the settings guard records the identical residual for
`settings.json`. Note that the configurator, whose recipe is the one that used to hand-edit
this file, has no `Bash` at all.

Where the backstop errs it errs closed, and the breadth is the cheap direction: a shell write
to *any* file of that name is refused wherever it lives, and so is a read spelled with a verb
the small list does not name — `sed -n p` reads, `sed -i` writes, and it does not try to tell
them apart. The way through is the file tools, whose paths are literal and resolved exactly.
The reads the recipes actually call for — `cat`, `grep`, `head`, with a benign `2>/dev/null`
— pass.

The red case is the file-tool half inverted: an `Edit` of a resident's trigger file that is
allowed, or a trigger tool that awaits between reading the document and writing it.

What it does not cover: the operator's own edits, deliberately; `config_sync`'s worker
thread, which rewrites the same file with no coordination (#458); the shell residual above;
and an alias the check cannot see through — a hard link, or a symlink retargeted after it
(#460). The claim is about the route an agent is actually told to take, and that route is
closed exactly.

## Failure behavior

A refused write is denied before the tool runs, and the denial's reason (`hooks.py::make_trigger_file_write_guard`) names `config_trigger_upsert` and `config_trigger_delete`, the typed tools that make the change inside Casa under the lock below.

## Extension points

A new writer of a resident's trigger file takes `trigger_write_lock.PASS_LOCK` across its whole read-modify-write, as the three writers INV-TRIG-011 names do.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/hooks.py::make_trigger_file_write_guard`

**Tests**
- `tests/test_hooks.py`
- `tests/test_config_sync_reminder_race.py`

**Related**
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/reminders.md`](../architecture/reminders.md)
- [`architecture/config-reconciliation.md`](../architecture/config-reconciliation.md)
<!-- END SOURCEMAP -->
