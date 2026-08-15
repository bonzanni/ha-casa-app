---
last_reviewed: 2026-08-15
---

# Engagement containment

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The OS-level boundary around a `claude_code` executor engagement: the uid it is given and
never shares, the workspace that uid owns, how root reaches into that workspace without
being redirected out of it, where the run-state root alone touches lives, the preflight that
refuses a launch whose privilege drop could not succeed, and the confirmed-down sweep boot
replay runs before it migrates or resumes a service.

The engagement *record* — creation, the terminal transition and its side effects, the
completion gate, turn admission, what a restart rewrites — is
[`architecture/engagements.md`](engagements.md); the finalize path's driver teardown, the
one stop path that is not a boot path, is described there. Two neighbouring boundaries live
elsewhere: the hook floor bounding what the CLI may *do* once running is in
[`architecture/hook-resolution.md`](hook-resolution.md), and the file modes that keep
credentials out of a dropped uid's reach are INV-STATE-005 in
[`architecture/persistent-state.md`](persistent-state.md) — both the allocator and the
preflight refuse while those modes are wrong, and that refusal is declared there.

Specialist and legacy engagements are out of scope throughout: their records carry
`UNALLOCATED_UID`, their workspaces are never chowned, and they never drop privilege.

## Mental model

**A `claude_code` engagement gets its own OS identity, not just its own record.** A
never-reused uid, backed by a durable dual-copy counter, is allocated per engagement, its
workspace chowned to it and reachable by root only through a no-follow accessor, and the run
script's final `exec` drops privilege via `setpriv` — see the invariants below for the rest.

**Every step of that is fail-closed, because the wrong uid is worse than no uid.** Handing
out a uid some live process already holds lets one engagement act as another's identity;
dropping into a uid that can still read a Supervisor token hands an engagement every app's
stored secrets. So each step refuses rather than degrades: the allocator poisons itself on
any failure it cannot bound and refuses every later allocation, the run-script render refuses
to substitute a sentinel or a below-base uid, the preflight refuses to plant a service whose
drop it cannot prove, and boot replay keeps services down rather than starting one that would
run as root or crash-loop under its supervisor.

**Two directories, not one, and that is what removes the symlink primitive.** The uid-owned
workspace holds what the engagement's own CLI reads and writes by path. Every file only root
touches — the captured session id, the spawn-epoch fence, the per-epoch stderr rings, the
inbound spool, the stdin FIFO, the stream cursor, the cached executor-memory block, the
record's crash-recovery metadata — lives instead in a root-owned control directory the CLI is
never `--add-dir`ed into. A symlink the CLI plants in its own workspace cannot redirect
root's read or write of any of them, because none of them is a path under the workspace at
all.

**Root still reaches in, and that reach is the boundary's remaining weak point.** casa-core
stays root and has to read and write inside a workspace owned by the very process the drop
exists to contain, so those crossings go through the confined accessors rather than a plain
`open()` on a joined path.

**The boundary is enforced at start and at boot; it is not re-established by a process
ending.** A launch is gated by the preflight. A boot sweeps every engagement service before
it migrates or resumes anything, each through a bounded ladder whose last rungs attempt a
group `SIGKILL`, and kills escaped descendants still holding the engagement's uid
best-effort as it goes; a service that will not confirm down is refused rather than started,
and blocks only its own engagement. Between those two points the guarantee is the uid
itself: whatever runs under it can reach only what that uid can reach. Stopping a *running*
engagement is the finalize path's driver teardown, which belongs to the record's lifecycle
rather than to this boundary — INV-CONT-001's scope clause below states what that leaves
uncontained.

## Contracts & invariants

**INV-CONT-001**: A `claude_code` engagement's uid comes from two independently-written durable high-water copies and is never handed out twice, even if either file is lost.

Reconstruction takes the max of every still-valid durable copy, raised (never lowered) by
records, dir owners, `casa-eng-*` passwd, and `/proc` ids. With both copies lost, a
never-removed `initialized` marker present — OR absent but a real uid (`>= UID_BASE`) evidenced —
poisons (a uid was allocated); marker absent with no real uid inits at base. A copy that is
present but unreadable poisons when no valid sibling copy remains.

What it does not cover: specialist and legacy `EngagementRecord`s carry `UNALLOCATED_UID` and
are never chowned or uid-dropped — this applies only to `claude_code` executors. And a uid is
not a container: a process that is root, or that kept `CAP_DAC_OVERRIDE`, is root-equivalent
whatever uid it carries, so the boundary bounds only what an ordinary dropped process can
reach. Two survivor classes follow from that and are mitigated rather than closed — a legacy
root process from before the drop existed, killed best-effort by the boot sweep below, and a
process still running under the engagement's own uid after its record has gone terminal,
where the finalize path issues the service stop without the checked ladder the boot path
uses, so nothing confirms extinction
([#599](https://github.com/bonzanni/ha-casa-app/issues/599)).

**INV-CONT-002**: Root's own read and write accessors for engagement workspace files refuse to follow a symlink at the final path component or any intermediate one, and can require the resolved file's owner to match an expected uid.

Enforced by `safe_fs.py`'s `open_beneath`/`read_text_beneath` and `atomic_write_beneath`,
preferring `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, falling back to an
FD-relative `O_NOFOLLOW` walk on a kernel without it.

What it does not cover: root's own reachability into a workspace it already has filesystem
access to — not a boundary between two non-root uids, which ordinary file permissions after
`chown_workspace` enforce instead.

**INV-CONT-003**: Root-touched engagement run-state is never joined into the uid-owned workspace root a `claude_code` engagement's own CLI process can reach.

That root is reachable by the subprocess via `--add-dir`, so a control-only file placed there
would be a symlink-planting target for the process the uid drop exists to contain. Every root
module touching run-state is held to a fixed allowlist of control-only basenames that must
never be path-joined to a workspace-root symbol.

What it does not cover: a static, symbol-name-based check over a fixed set of root modules,
not a full dataflow analysis — it catches established naming for "the workspace root," not an
arbitrarily renamed variable.

**INV-CONT-004**: A `claude_code` run script is never rendered or planted while its uid drop cannot be established — no `setpriv`, an unallocated uid, a workspace not owned by that uid, no passwd entry for it, or a pinned plugin directory neither owned by that uid nor world-readable and traversable — and the service is refused rather than started as root or left to crash-loop under its supervisor.

Checked immediately before the render, so a chain that would otherwise fail at
`setpriv`-exec time — or worse, silently exec without dropping — is caught earlier, named.
The two paths reach the checks differently: a fresh launch calls the driver's preflight,
while boot replay states its own gates in order — one `setpriv` gate covering every resume
that boot, then per-record passwd regeneration and the workspace chown, then the shared
plugin-directory check — each refusing that record rather than starting it.

What it does not cover: the checks establish the *conditions* a drop requires; they do not
verify the exec'd `claude` process ended up with the expected `Uid`/`CapBnd`, a live property
confirmed operationally. Nor is the plugin-directory check reached on every restart: a record
whose service pair is intact and current is resumed from the pair already on disk, without
the re-render that check guards, so a pinned directory whose mode changed since that render
surfaces as the dropped CLI failing to read it rather than as a refusal.

**INV-CONT-005**: Boot replay migrates a legacy root run-script to the uid-dropped form, or resumes an existing one, only after confirming the corresponding s6 service is fully down; an unconfirmed-down service refuses the resume.

The confirmation scans the supervised service tree itself rather than trusting the durable
record, so a service still up — including one with no matching "undergoing" record — is
never migrated or restarted out from under itself. Only once that sweep has run does replay
re-fold live `/proc` uids into the high-water, before any uid backfill or `setpriv` render.

What it does not cover: the sweep is per-service, so one engagement's unconfirmed service
refuses that engagement's resume and leaves the rest of the boot to proceed. And "confirming
down" is not a passive observation — the ladder re-issues the stop, latches the service down
so its supervisor cannot revive it, then attempts a bounded group `SIGKILL` through the
supervisor and, if that leaves the service up, a direct kill of the leader's process group —
or of the leader alone, where it is not its group's leader. No rung is trusted to have
worked: a rung that lands is followed by the strict probe, and a kill that cannot even be
delivered ends the ladder in refusal without one. A service the ladder does not confirm down
either way is refused rather than started.

## Failure behavior

**The allocator cannot prove its high-water.** Any failure it cannot bound — a durable copy
present but unparseable with no valid sibling, an unscannable `/proc`, both durable writes
failing, or the marker-and-evidence rules meeting a state in which a uid was allocated whose
maximum is unprovable — poisons the allocator and raises `UidStateError`. A poisoned allocator
refuses *every* later allocation, so no `claude_code` engagement is created and no legacy
record is backfilled until a successful reconstruction clears it. Only reconstruction clears it: the
live-`/proc` refold refuses outright while poisoned, so it is a way in, never a way out. The
repair is restoring a durable copy — there is no reset, because resetting the counter is how
a live uid gets reissued.

**The preflight refuses a launch.** `UidDropRefused` is raised, the s6 service is neither
planted nor started, and there is no root fallback. The engagement does not start and the
refusal names its condition, rather than surfacing later as an exec failure or a silently
undropped process.

**`setpriv` is missing from the image at boot.** Boot replay refuses every `claude_code`
resume that boot rather than a subset, and the services stay down from the pre-migration
sweep — never started as a root service, never left crash-looping under their supervisor.

**A service will not confirm down.** By then the ladder has re-issued the stop, latched the
service down and attempted its kill rungs, and it is still not confirmed down — either the
strict probe will not call it down, or the last kill could not be delivered at all — its pid
unreadable, refused by the kernel, or already gone. Replay refuses to
migrate or resume that engagement, marks its record errored best-effort, and never adds it
to the start loop this boot. Every other engagement proceeds; the refusal is scoped to the
one service.

**Root's accessor meets a symlink.** The read or write raises `SymlinkRefused`, an `OSError`
subclass, and the caller decides what a refused file means — the accessor never resolves
through it. On a kernel without `openat2` the same refusal comes from the fd-relative
`O_NOFOLLOW` walk, which is a second implementation of the guarantee rather than a weaker
one.

**A file resolves cleanly but is owned by the wrong uid.** With an expected owner supplied,
it is refused on the `fstat` of the final descriptor — so root reading a workspace file back
is reading one the engagement's own uid wrote, not one substituted by something else.

## Extension points

**A new root-side read or write of an engagement file** goes through the confined accessors
with the record's uid as the expected owner, never a plain `open()` on a joined path. Root's
crossings are the surface this boundary exists to narrow, and a plain open follows whatever
the workspace's owner planted.

**New run-state that only root touches** gets its basename under the control directory, and
joins the inventory check's control-only allowlist so the static scan keeps covering it. A
name the check does not know is a name it cannot notice being joined to the workspace root.

**A new evidence source for the high-water** is folded into the reconstruction, where it may
only raise the mark. A source that cannot be read has to poison rather than be skipped — a
skipped source is indistinguishable from one that found nothing, and that difference is the
whole non-reissue guarantee.

**A new precondition for the uid drop** has to be added on every path that renders a run
script, and it is worth checking which those are before assuming two. The driver's preflight
covers a fresh launch; boot replay does not call that preflight but states its own gates, and
one of its paths — an intact, current service pair — resumes without re-rendering at all, so
a precondition placed only in the render is not evaluated there. A condition added to one
path is a hole the others walk straight through.

**A new flag or export in the run script** is added to the template, which every render
substitutes and whose final line is the `setpriv` exec. Anything appended after that exec
never runs, and anything that replaces it drops the privilege drop with it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_uids.py::UidAllocator`
- `casa/rootfs/opt/casa/engagement_uids.py::UidAllocator.reconstruct`
- `casa/rootfs/opt/casa/safe_fs.py::open_beneath`
- `casa/rootfs/opt/casa/safe_fs.py::read_text_beneath`
- `casa/rootfs/opt/casa/safe_fs.py::atomic_write_beneath`
- `casa/rootfs/opt/casa/drivers/workspace.py::provision_control_dir`
- `casa/rootfs/opt/casa/drivers/workspace.py::render_run_script`
- `casa/rootfs/opt/casa/drivers/workspace.py::chown_workspace`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::_preflight_uid_drop`
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`
- `casa/rootfs/opt/casa/scripts/engagement_run_template.sh`

**Tests**
- `tests/test_engagement_uids.py`
- `tests/test_safe_fs.py`
- `tests/test_root_workspace_accessor_inventory.py`
- `tests/test_boot_replay.py`
- `tests/test_claude_code_driver.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
