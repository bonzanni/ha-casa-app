---
last_reviewed: 2026-09-21
---

# Plugin file handoff

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The folder through which a file moves from one plugin to another, or from Casa to a plugin:
its layout, the producer's `publish` and the consumer's `capture` (`casa_handoff.py`, the
contract plugins vendor), Casa's provisioning and hourly sweep of it (`plugin_handoff.py`),
and the handoff side of `share_inbound_file`, which passes a file the operator sent in
Telegram to a plugin. How that file arrived and who may read it in the inbox is
[`inbound-files.md`](inbound-files.md); plugin media sent to the operator travels through a
different directory, the outbox in [`plugin-runtime.md`](plugin-runtime.md).

## Mental model

**One folder, owned by Casa, written by producers.** The folder is `/data/handoff`,
relocatable by `CASA_HANDOFF_DIR`. Casa creates it at boot with mode `0770`, right after the
plugin outbox and before any agent turn can run. A producer — a plugin, or Casa itself for
`share_inbound_file` — publishes a file and hands the returned absolute path to whoever
needs it. A consumer that accepts handoff files takes one with `capture` and uses the bytes
it returns.

**The layout is the contract.** A published file lives at
`<root>/<producer>/<id>/<filename>`. `<producer>` is a lower-case name (`casa` for Casa's
own publications). `<id>` is `<13-digit epoch ms>-<16 hex>`, and its epoch is the file's
age clock. `<filename>` is the human name — the reason a file keeps its name when, say, it
is attached to an email — cleaned of control characters, separators and leading dots and
shortened, extension kept, to at most 128 characters and 200 UTF-8 bytes. A publication in
progress lives under `<root>/<producer>/.staging-<id>/` and becomes visible by one rename of
the whole directory, after the file and the directory were flushed.

**Plugins vendor the contract.** `casa_handoff.py` is stdlib-only and self-contained so a
plugin can copy it verbatim; Casa imports the same file. A plugin running against a Casa
that has no folder gets `handoff_unavailable` from `publish` rather than a folder it created
itself.

**Reading never deletes; age does.** `capture` leaves the file where it is, so one
published file can be taken by several consumers or several times. The sweep removes it
seven days after the epoch in its `<id>`. Retention follows the system clock: a clock that
jumps forward shortens it, a clock that jumps back lengthens it, and nothing re-dates a file,
since the age is read from its name rather than from `mtime`.

**Full means refuse, never evict.** A publication is refused when the file is over 25 MB,
when the folder would then hold more than 2 GB, or when it would leave less than 128 MiB free
on the filesystem that holds the folder. Nothing is ever removed to make room — neither by
the producer nor by the sweep — so the next publish is refused until files age out.

**What this is not.** Every in-Casa plugin runs as the same user as Casa, and can read any
file in the folder, or anywhere else Casa can, directly. Producer directories are a
convention between honest plugins, not an access control. Credentials never transit the
folder: that is a rule for plugin authors, and nothing here inspects content. What `capture`
does guarantee is narrower and mechanical — that a path handed to a consumer is not taken
from *outside* the folder (INV-HANDOFF-001), which is the outcome that would turn "attach
this file" into "mail this credential".

**Uid-dropped executors cannot see it.** The folder is `0770` and owned by root, the user
Casa runs as; a `claude_code` executor engagement runs under its own dropped uid and gid with
its supplementary groups cleared, so its plugins can neither publish into the folder nor
capture from it.

**Sharing an inbound file copies it.** `share_inbound_file(path)` takes a path exactly as
`list_inbound_files` shows it, publishes a copy as producer `casa` under the operator's
filename, and returns the handoff path plus the seven-day retention. The inbox copy is
untouched and keeps its own retention. A file with no recorded operator name is named by its
kind: an image as `photo.<ext>`, any other file (a document whose display record is missing)
as `file.<ext>`. The tool's description
tells the agent to share whenever a plugin tool needs one of those files, without asking the
operator first. The shipped defaults grant it to the `assistant` resident only, in both its
agent runtime and its role.

## Contracts & invariants

**INV-HANDOFF-001**: `capture` returns bytes only for a path that resolves to exactly `<root>/<producer>/<id>/<filename>` under the real root and whose opened descriptor is a regular, single-link file, read from that same descriptor; anything else is refused as `not_a_handoff_file`.

The path is resolved first, the layout is judged on the resolved path, and that path is
opened without following a final link and without blocking, so the checks and the read see
one file. A final symlink, a hard link to a file elsewhere, a FIFO or device, a traversal, a
producer directory linked out of the folder, a sibling folder whose name merely starts like
the root, and a path inside a `.staging-` directory are all refused; a file over 25 MB is
refused as `file_too_large`. A consumer uses the returned bytes and never opens the path
again.

What it does not cover: a process running as Casa's user can write, replace or read any
file in the folder between two calls, since that user can do so anywhere. The guarantee is
about where the bytes came from, not about who put them there.

**INV-HANDOFF-002**: `publish` writes the producer's bytes into a new file it creates exclusively, never linking or renaming a source into the folder and never following a link to one, and when the file, the folder total or the disk reserve would be exceeded it refuses without removing anything; a failed publication leaves no staging directory behind.

A source file is read through the same descriptor checks as `capture`, so a symlinked
source is refused before anything is written.

What it does not cover: the folder total is checked by each producer before it writes and
is not a lock across producers, so two concurrent publications can together take the folder
over 2 GB. The sweep logs that at WARN and still evicts nothing.

**INV-HANDOFF-003**: The sweep never removes a conforming published file whose `<id>` epoch is less than seven days old, and it removes every non-conforming entry — logged at WARN — without following a link.

Non-conforming means: a non-directory, or an off-pattern name, where a producer directory
belongs; an `<id>/` that does not hold exactly one regular single-link file under a valid
name; a `.staging-` entry that is not a directory with a valid id; a symlink at any level.
A `.staging-<id>/` older than an hour is an abandoned publication and is removed. A
conforming `<id>/` dated more than an hour in the future is kept, with a WARN, so a backward
clock correction cannot delete a legitimate file.

**INV-HANDOFF-004**: `share_inbound_file` publishes only a file that the executing agent's own inbox currently lists, and `list_inbound_files` lists only that inbox; any other path, and any call from an agent without an inbox, publishes nothing.

The executing agent is the origin's `execution_role`, falling back to `role`. A delegated
agent carries its caller's `role` and its own `execution_role`, so keying on `role` alone
would hand a delegate its caller's files; keyed on the executing agent, a delegate is told
it has no inbound files, whatever path it passes.

## Failure behavior

**The folder cannot be provisioned at boot** — the path is not a real directory, or
creating it fails. Boot continues; `share_inbound_file` answers that the handoff folder is
not available, and a producer finds no folder and gets `handoff_unavailable`. Once the folder
exists it is available, because producers publish into any folder that exists: a first
sweep that fails is logged and left to the hourly pass, and an hourly sweep that cannot be
registered is logged, leaving the folder swept at boot only.

**A sweep pass fails.** It is logged at WARN and the next hourly pass tries again.

**A publication fails part-way.** The staging directory is removed and the producer gets
`storage_failed`; nothing becomes visible.

**The folder or the disk is full.** The producer gets `handoff_full`: for a full folder
the message names the seven-day retention, for a nearly full disk it says so;
`share_inbound_file` passes the refusal on to the agent.

## Extension points

**A new producer** needs only a name matching `^[a-z0-9][a-z0-9-]{0,63}$` and the vendored
`publish`; there is no registration. **A new consumer** must read through `capture` — any
other open of a handed path loses INV-HANDOFF-001.

**Changing a limit or the retention** changes the vendored contract: plugins carry their
own copy of the constants, so a change here reaches a plugin only when it re-vendors the
file. The retention also appears in `share_inbound_file`'s description and in the
user-facing app documentation, which change with it.

**Granting `share_inbound_file` to another agent** is a decision that agent may pass the
operator's files to plugins; it is useful only to an agent that has an inbox.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/casa_handoff.py`
- `casa/rootfs/opt/casa/plugin_handoff.py`
- `casa/rootfs/opt/casa/tools.py::share_inbound_file`
- `casa/rootfs/opt/casa/tools.py::_inbox_for_executing_agent`

**Tests**
- `tests/test_plugin_handoff.py`

**Related**
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/plugin-runtime.md`](../architecture/plugin-runtime.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
<!-- END SOURCEMAP -->
