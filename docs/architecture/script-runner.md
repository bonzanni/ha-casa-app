---
last_reviewed: 2026-11-10
---

# The script runner

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The scratchpad's `run` operation ([`scratchpad.md`](scratchpad.md)): a model's Python script,
run once over its pad's sections in a confined process and written back to the pad. The
box — the AppArmor child profile `pyrun` in `casa/apparmor.txt`, the dedicated interpreter
and the prelude under `/opt/casa/run`, the uid drop, the limits and the syscall filter; what
each refuses and what observes it on every run; the digest that ties the loaded profile to the
code; Casa's side in `pad_run.py` (slots, wall clock, the frame rule, the result's shape, the
apply under the pad's stamp, the reply); the kill-by-uid ladder in `uid_kill.py`; and what
`boot` proves before `run` is offered. The pad itself, its key and the route are
[`scratchpad.md`](scratchpad.md).

## Mental model

**A model that has read a page writes the script, so inside the box it is free and the
boundary is tight.** The script gets a global `pad`, a dict of section name to text in pad
order (`""` is the lines above the first mark, always present), any standard-library module,
`print`, and nothing else: no network, no file outside the interpreter's own, no other
process, no thread, no event loop, 30 s of wall clock, 20 s of CPU, 512 MiB. It changes,
adds, removes or renames sections by changing the dict, or binds a new dict to `pad`; the
final binding is what comes back, and only on exit 0, all at once.

**The boundary is a child profile the kernel enforces, plus what the profile cannot
mediate.** Casa's main process is root under the app's profile. `pad_run.execute` spawns
`setpriv --reuid <uid> --regid <uid> --clear-groups --bounding-set -all --inh-caps -all --
/opt/casa/run/python3 -I -S -B /opt/casa/run/child.py <Casa's pid> <expected label> <uid>`
with an empty environment, descriptors 0, 1 and 2 only, in its own session. The interpreter
is a real copy of the system one at an exact path, and the profile's rule
`/opt/casa/run/python3 cx -> pyrun,` outranks `/opt/casa/** rix`, so that one path, and no
other, transitions into the child. The child profile has no network rule (with the `abi`
line every socket family is refused, unix and socketpair included), no exec rule, no mount,
ptrace or capability rule, and read access only to the interpreter, the prelude, the C
libraries, the standard library, the locale data, glibc's charset table (the `gconv-modules`
files only, which the C library reads as the interpreter starts; no converter is loadable),
zoneinfo, `/etc/localtime`,
`/etc/ssl/openssl.cnf`, a process's confinement label and its own descriptor listing. A
`--no-new-privs` flag is deliberately absent: the kernel refuses a profile transition under
`no_new_privs`; the prelude sets the bit itself after the transition.

What the profile does not mediate, the prelude's seccomp **denylist** refuses with `EPERM`
(the table below), and the uid, the limits and the empty capability sets close the rest. The
uid is one of two slots, 199000 and 199001, above every image uid and below the engagement
band; `uid_kill.UID_FLOOR` is the floor the ladder looks at.

**The prelude proves every refusal on its own process, every run, before the script.** In
order (`child.check_box`): the parent-death signal, then the parent is Casa's pid; real,
effective and saved uid and gid all the slot's, no supplementary groups, `capget` all zero,
the environment at most `LC_CTYPE`; the confinement label read from `/proc/self/attr` is
exactly `<Casa's name>//pyrun (enforce)`; the sentinel `/opt/casa/run/pyrun.<digest>` opens;
an `AF_UNIX` and an `AF_INET` stream socket both raise `PermissionError`; `/etc/passwd` and
`/proc/1/cmdline`, world-readable, both raise `PermissionError` (a missing file proves
nothing and refuses too); an open of `/proc/self/fd` lands on 3 and, closed again, the
listing is exactly `0 1 2 3`; `nice` moves to 10; the nine rlimits set (`AS` 512 MiB, `CPU`
20/25 s, `FSIZE` 0, `NOFILE` 32, `CORE` 0, `MEMLOCK` 0, `NICE` 0, `RTPRIO` 0, `NPROC` 1 last);
`no_new_privs`, the filter installed, and four calls it closes — `unshare(CLONE_NEWUSER)`,
`execve("/nonexistent")`, `shmget`, `add_key` — each `-1` with `EPERM`. Any failure is one
`box:` line on stderr and an exit 100-109 with no script run; anything unexpected before the
frame is exit 107. Then it writes the **ready frame** `{"box": "ready"}` as stdout's first
line, reads the payload from stdin, runs the script with stdout replaced by a capped capture,
and writes the result `{"printed", "printed_total", "pad"}` as stdout's last line, ASCII
only, so a lone surrogate travels as its escape. The two socket and two file refusals are
intentional: a normal run leaves exactly those four `DENIED` lines in the kernel's audit
from the box's pid and no other, and a rule is never added for one of them — the prelude
refuses to run when one is allowed.

**No ready frame means the box failed, whatever the exit status.** A prelude refusal,
`setpriv`'s own 126 or 127, a traceback before the frame, a kill during the prelude: `run`
becomes unavailable until restart with one ERROR carrying the stderr tail and one operator
notice per process (`pad_run._unavailable`); the pad is untouched. Exit 104 — the prelude
found a parent that is not Casa — is logged and does not retire `run`, since it means this
process was gone. After the frame every status is the script's, whatever its number: a
script's `os._exit(100)` is "Exit 100: the script's own exit", nothing more.

**The sentinel ties the loaded profile to the code.** The Supervisor loads `apparmor.txt`
only on a version change followed by an update; a same-version rebuild, a restart or a
rollback can leave a child profile of another text in the kernel under the same label. The
child profile's rule names `/opt/casa/run/pyrun.<digest>`, where the digest is the first 12
hex digits of sha256 over the child profile's text with that one line left out
(`scripts/pyrun_digest.py` is the one place the convention lives: `print` for the Dockerfile,
which creates the file at build time; `check` holds the rule, `child.py`'s `PROFILE_DIGEST`
and the text together, refuses any other rule of the child text whose path could match a
sentinel, and runs in the pre-push gate). A child of another text allows only its own name,
so the open is refused in both directions and the box refuses with exit 106.

**Casa's side (`pad_run.run`).** Before the spawn: `run` is ready and available, the pad is
not already running, a slot is free (parked slots get the ladder again first), the pad's
snapshot is taken with its stamp and each section's header line kept aside
(`snapshot_sections`; a name two sections share refuses, and so does a `## ` header whose name
is empty, since the top and that section would be one key to the script). The payload is ASCII
JSON. Stdout is read to 8 MiB (the result's bound for a full pad is 6.23 MiB) and drained past
it with nothing kept, not even a record of a chunk; stderr
keeps its last 4 KiB; the wall clock kills at 30 s; the ladder kills the slot's uid after
every run, and a slot whose kill is not verified `extinct` is parked with an ERROR. At exit 0
Casa checks the result's shape itself, whatever the child said — a dict of exactly `printed`
(str), `printed_total` (int) and `pad` (null, or a list of `[str, str]`) — and anything else,
null included, is "The result could not be read". `lines_from_result` turns the pairs back
into lines in one pass: two keys that name one section, the top included, refuse first; a key
the script kept takes its section's header line exactly as it was, a key that cleans to `""`
is the top, any other key gets `## ` + its cleaned name, and a line of a value beginning
`## ` is stored with one leading space. A last line that is not JSON, or nested past what the
decoder walks, is "could not be read" like any other unreadable result. `PadStore.apply` then refuses when the
pad is gone or its stamp moved (every creation and every change moves it — a write, the
route, a brief, a move, an expiry and recreation — so a pad changed and restored with the
same lines is still another pad to the run), passes the same door as every change (no second
section of a name, never past 1 MiB), and assigns the lines once under the lock.

**The reply**, inside the pad's bound: the shape; the page-text sentence (printed output is
page text as much as a read is); one status line — `Ran 0.3 s, exit 0.`, `Exit 1: the script
raised.`, `Exit 3: the script's own sys.exit.`, `Killed after 30 s.`, `Stopped after 20 s of
CPU.`, `Killed by signal 31 (SIGSYS: …)`, `The result could not be read.`, `run is unavailable
on this install; Casa's log says why.` — with `Nothing changed.` on every outcome but the
first; on success the section summary, at most ten names in all with counts for the rest; the
printed head cut at `chars` with the true total; the stray bytes a script wrote to the real
stdout; and on a non-zero exit the last 2,000 characters of stderr.

**Boot.** `run` is not ready until `pad_run.boot` returns. It derives the expected child
label from Casa's own (`<name> (enforce)` becomes `<name>//pyrun (enforce)`; anything else is
unavailable from the start), requires `x86_64`, runs the ladder on both slot uids, then runs
`pass` over no sections through the whole box, which must answer the frame, exit 0 and an
empty pad. Any failure leaves `run` unavailable with the fix named in one ERROR and one
operator notice: update the app so the profile loads (label), remove the stale child profile
(sentinel), or the box's self-check failed. A failure found before the Telegram channel's
first connection keeps its notice until the channel is ready; the boot does not wait for it.
The preflight is not trusted later: the prelude's
checks are the first code of every run.

### The x86_64 syscall surface, by class

The filter refuses 105 numbers with `EPERM`, allows every other number, and kills the process
on any x32 number or any other architecture. Each row says what mediates the calls that stay
allowed. A call missed by the list that reaches no asset — no secret or file content, no
socket, no other process, no other pad's content — is a recorded residual, not a gap.

| Calls | Mediated by | Decision |
|---|---|---|
| SysV shm, msg, sem; POSIX mqueue | nothing | filter |
| keys (`add_key request_key keyctl`) | nothing | filter |
| io_uring | file hooks on the opens it submits | filter |
| `unshare setns clone clone3 fork vfork` | `RLIMIT_NPROC 1` refuses task creation; `unshare` is not one | filter |
| `execve execveat` | AppArmor: no exec rule | filter; `execve("/nonexistent")` must be `EPERM`, not `ENOENT` |
| mounts, `open_tree`, `move_mount`, `fs*`, `pivot_root`, `chroot`, swap | AppArmor mount class, capabilities a user namespace would grant | filter |
| `utime utimes futimesat utimensat` | DAC only: a world-writable directory's mtime is a timestamp of reach | filter |
| `ptrace process_vm_* kcmp process_madvise process_mrelease pidfd_* move_pages migrate_pages perf_event_open bpf userfaultfd` | uid mismatch, AppArmor ptrace class, sysctls | filter |
| inotify, fanotify | DAC only: a watch on `/config` would yield names | filter |
| `name_to_handle_at open_by_handle_at` | file hooks on the final open | filter |
| `memfd_create memfd_secret` | `RLIMIT_FSIZE 0` on writes | filter |
| aio (`io_*`) | a system-wide limit any container shares | filter |
| `syslog acct reboot kexec_* *_module sethostname setdomainname settimeofday clock_settime adjtimex clock_adjtime ioperm iopl vhangup uselib lookup_dcookie quotactl* seccomp lsm_set_self_attr` | capabilities, all empty | filter |
| sockets | AppArmor network class, deny-by-default under abi 3.0 | allowed: the two refused sockets observe the class |
| files (`open* read write stat* getdents* *xattr ioctl …`) | AppArmor file class (`r`/`m` only), DAC, `RLIMIT_FSIZE 0`, `RLIMIT_NOFILE 32` | allowed: the stdlib needs them |
| signals | DAC (every other uid refused; the same-session `SIGCONT` exception closed by the new session), AppArmor signal class | allowed: harmless to self |
| memory (`mmap mprotect brk mlock* …`) | `RLIMIT_AS`, `RLIMIT_MEMLOCK 0` | allowed |
| self-only (`prctl futex* rt_sig* sched_* get*id set*id pipe* dup* getrandom sync syncfs …`) | the uid held, empty capabilities, `RLIMIT_NICE 0` | allowed |

**What the box does not stop, on purpose.** A stat of any path (AppArmor does not mediate
getattr): a script learns that a path exists, its size and mtime, never its content; `xattr`
reads, `O_PATH` opens and `file_getattr` are the same metadata; `sync`/`syncfs` and
`ioprio_set` are I/O nuisances inside the CPU budget; any process's confinement label is
readable (every label in the container is Casa's); CPU for up to 25 s at `nice` 10 on one
core per slot; one kernel audit line per refused open for a hostile script's whole budget;
a script that clears its own parent-death signal and sleeps while Casa is down stays confined
and does nothing, and `boot` kills both slot uids before the next run. `ctypes` can call any
libc function, and every such call ends in the same kernel checks, so it adds no reach.

## Contracts & invariants

**INV-PAD-012**: A `run` changes its own pad only on exit 0, all at once, only when the pad's stamp is the snapshot's, never past the cap, round-trips an unchanged pad byte for byte (header spelling, blank lines and lone surrogates included), and its reply stays inside INV-PAD-002.

Enforced by `pad_run.run` (the no-frame rule, the status mapping, `_well_formed`,
`lines_from_result`), `PadStore.snapshot`, `PadStore.apply` and the stamp every creation and
change path moves, and `child.run_script` with its ASCII write.

What it does not cover: a bound job pad whose file write fails after a successful apply keeps
the change in memory, as every operation on it does.

**INV-PAD-013**: The box runs no script unless its label is Casa's with `//pyrun (enforce)`, the sentinel named by the child profile text's digest opens, a socket, a world-readable file, a user namespace, an exec, a SysV segment and a session key are refused, no descriptor above 2 is open, its parent is Casa, and its identity is the slot's uid with empty capabilities; and a run with no ready frame is the box's failure whatever its status.

Enforced by `child.check_box` before the frame, by `pad_run._reply`'s frame rule, by
`pad_run.boot`'s label derivation and preflight, and by `scripts/pyrun_digest.py check` in
the gate and the tests.

What it does not cover: a developer's machine has no AppArmor, so a green suite proves the
plumbing and the fail-closed paths, never the confinement; the host is where the label, the
four refusals, the limits, the kill and the audit's four lines are observed.

**INV-PAD-014**: Every run ends with a kill of its slot's uid, and a slot is reused only after a kill the ladder verified.

Enforced by `pad_run._release_slot` after every `execute`, at boot for both uids, and by
`_take_slot`, which retries the ladder on a parked slot and takes only a free one (a retry
cancelled or failed mid-ladder leaves the slot parked, never claimed and never free);
`uid_kill.kill_uid_until_empty` claims extinction only on two consecutive empty scans.

## Failure behavior

**The profile did not load, is in complain mode, or the transition rule is gone.** The label
check refuses (exit 100): `run` is unavailable from the first run, or from boot, with "update
the app so the profile loads". **A child profile of another text is loaded.** The sentinel
check refuses (exit 106): "remove the stale child profile, or update the app so the profile
loads". **The child was removed from the kernel while Casa runs.** The exec fails, `setpriv`
exits 126 with no frame: unavailable. **A script sleeps past the CPU limit.** The wall clock
kills it at 30 s; the ladder verifies the uid empty. **A process survives the kill.** The slot
is parked with an ERROR naming the survivors; the next run retries the ladder; with both
slots parked `run` is unavailable until one clears or Casa restarts. **Casa dies.** The
parent-death signal kills the box; the next boot's ladder covers what it missed. **The result
is unreadable** (past 8 MiB, a forged or malformed last line, `pad` null): nothing is applied
and the reply says so. **The pad changed during the run**: the apply is refused and the reply
says the pad changed. **The host is not x86_64**: unavailable from boot.

## Extension points

A rule for the child profile is added to the `pyrun` block in `casa/apparmor.txt` and its
digest regenerated with `scripts/pyrun_digest.py print` into the rule and `child.py`'s
constant — `check` refuses the tree otherwise — and ships with a version bump, since only an
update loads the profile. Another architecture needs its own syscall numbers and
`AUDIT_ARCH` in `child.py` and the whole host list observed there first. The denylist is
closed: a new reach is answered by naming what mediates it, or recorded as a residual.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/pad_run.py`
- `casa/rootfs/opt/casa/run/child.py`
- `casa/rootfs/opt/casa/scripts/pyrun_digest.py`
- `casa/rootfs/opt/casa/uid_kill.py::kill_uid_until_empty`
- `casa/rootfs/opt/casa/uid_kill.py::live_pids_for_uid`
- `casa/rootfs/opt/casa/scratchpad.py::PadStore.snapshot`
- `casa/rootfs/opt/casa/scratchpad.py::PadStore.apply`
- `casa/apparmor.txt`

**Tests**
- `tests/test_pad_run.py`
- `tests/test_run_child.py`
- `tests/test_pyrun_digest.py`
- `tests/test_run_image.py`
- `tests/test_uid_kill.py`
- `tests/test_apparmor_profile.py`

**Related**
- [`architecture/scratchpad.md`](../architecture/scratchpad.md)
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
<!-- END SOURCEMAP -->
