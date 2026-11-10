"""The script runner's prelude: what runs inside the box, around the script a model wrote.

Started as ``/opt/casa/run/python3 -I -S -B /opt/casa/run/child.py <casa pid> <label> <uid>``
by ``setpriv`` under the AppArmor child profile ``pyrun``, as the slot's uid, with an empty
environment and descriptors 0, 1, 2 only (``pad_run.py`` is the other side). Stdout's first
line is the ready frame ``{"box": "ready"}``, written after every check below passed and
before stdin is read; its last line is the result
``{"printed": str, "printed_total": int, "pad": [[name, text], ...] | null}``. Stdin carries
one JSON document ``{"script": str, "sections": [[name, text], ...]}``, each text being the
section's lines each ended by a newline.

Exit status: 0 the script finished (``pad`` is what to apply); 1 it raised; 2-99 its own
``sys.exit``. No ready frame means the box failed, whatever the status: 100-109 with one
``box:`` line on stderr when a check refused, anything else when it never got that far.
After the frame every status is the script's, whatever its number: a script's own
``os._exit(100)`` is its exit, not a refusal.

Every refusal the box relies on is observed on this process before the script runs, never
assumed from the boot preflight: the label, the sentinel named by the profile text's digest,
a socket of two families and two world-readable files refused by the profile, the
descriptor listing, the identity, the limits, and the syscall filter with four of its
refusals exercised. Stdlib only, from ``/usr/lib/python3.X``: the profile allows nothing else.
"""
import ctypes
import errno
import io
import json
import os
import resource
import socket
import struct
import sys
import traceback

PR_SET_PDEATHSIG, PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 1, 38, 22, 2
SIGKILL = 9
MiB = 1024 * 1024
PRINTED_MAX = 40_000           # characters of printed output carried back (scratchpad.READ_CHARS_MAX)
# The child profile's digest: 12 hex of sha256 over its text in casa/apparmor.txt with its own
# sentinel line left out (scripts/pyrun_digest.py). The Dockerfile creates
# /opt/casa/run/pyrun.<digest>, the profile allows exactly that path, and a test fails when
# any of the three disagree. A child profile of another text cannot open it.
PROFILE_DIGEST = "ee2f35704b81"
SENTINEL = f"/opt/casa/run/pyrun.{PROFILE_DIGEST}"
MUST_REFUSE_READ = ("/etc/passwd", "/proc/1/cmdline")   # world-readable: only the profile refuses
NICE = 10
LIMITS = (
    ("RLIMIT_AS", (512 * MiB, 512 * MiB)),
    ("RLIMIT_CPU", (20, 25)),
    ("RLIMIT_FSIZE", (0, 0)),
    ("RLIMIT_NOFILE", (32, 32)),
    ("RLIMIT_CORE", (0, 0)),
    ("RLIMIT_MEMLOCK", (0, 0)),
    ("RLIMIT_NICE", (0, 0)),
    ("RLIMIT_RTPRIO", (0, 0)),
    ("RLIMIT_NPROC", (1, 1)),      # last: nothing below needs a second process
)
# Every x86_64 call (asm/unistd_64.h) that creates a kernel object or a reach outside this
# process which the AppArmor child profile does not mediate, or that the script never needs
# (docs/architecture/script-runner.md has the table by class). EPERM; the filter kills any
# other arch and any x32 number. What stays allowed is mediated by the profile's file,
# network and signal classes, by DAC, by an rlimit, or reaches nothing outside the process.
DENIED_SYSCALLS = (
    29, 30, 31, 67, 68, 69, 70, 71, 64, 65, 66, 220, 240, 241, 242, 243, 244, 245,  # SysV, mqueue
    248, 249, 250,                                      # keys
    425, 426, 427,                                      # io_uring
    272, 308, 56, 57, 58, 435,                          # unshare, setns, clone, fork, vfork, clone3
    59, 322,                                            # execve, execveat
    165, 166, 155, 428, 429, 430, 431, 432, 433, 442, 457, 458, 467, 161, 167, 168,  # mounts,
    # open_tree_attr, chroot, swap
    132, 235, 261, 280,                                 # utime family: no AppArmor hook, DAC only
    101, 310, 311, 312, 440, 448, 434, 438, 424, 279, 256, 298, 321, 323,  # ptrace, process_vm_*,
    # kcmp, process_madvise/mrelease, pidfd_*, move/migrate_pages, perf_event_open, bpf, userfaultfd
    253, 294, 254, 255, 300, 301,                       # inotify, fanotify
    303, 304,                                           # name_to_handle_at, open_by_handle_at
    319, 447,                                           # memfd_create, memfd_secret
    206, 207, 208, 209, 210, 333,                       # aio
    103, 163, 169, 246, 320, 175, 313, 176, 170, 171, 164, 227, 159, 305, 173, 172, 153, 134,
    212, 179, 443, 317, 460,                            # syslog, acct, reboot, kexec, modules,
    # hostname, time setting, ioperm/iopl, vhangup, uselib, lookup_dcookie, quotactl, seccomp,
    # lsm_set_self_attr
)
CLONE_NEWUSER = 0x10000000
AUDIT_ARCH_X86_64, X32_BIT = 0xC000003E, 0x40000000

# Refusal codes: the box did not run the script.
LABEL, SOCKET, FILE, FDS, PARENT, IDENTITY, GENERATION, UNEXPECTED, LIMIT, FILTER = range(100, 110)


def refuse(code: int, why: str) -> None:
    sys.stderr.write(f"box: {why}\n")
    sys.stderr.flush()
    os._exit(code)


class _Capture(io.TextIOBase):
    """Replaces sys.stdout while the script runs: keeps the first PRINTED_MAX characters, counts
    the rest, so a print loop costs no memory and the real stdout stays clean for the result."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.kept = 0
        self.total = 0

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        s = str(s)
        self.total += len(s)
        room = PRINTED_MAX - self.kept
        if room > 0:
            self.parts.append(s[:room])
            self.kept += min(len(s), room)
        return len(s)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _bpf(code, jt, jf, k):
    return struct.pack("HBBI", code, jt, jf, k)


def seccomp_program() -> bytes:
    """Classic BPF: kill on any arch but x86_64 and on any x32 number; EPERM on the denylist;
    allow the rest."""
    deny = sorted(DENIED_SYSCALLS)
    prog = [_bpf(0x20, 0, 0, 4), _bpf(0x15, 1, 0, AUDIT_ARCH_X86_64), _bpf(0x06, 0, 0, 0x80000000),
            _bpf(0x20, 0, 0, 0), _bpf(0x35, 0, 1, X32_BIT), _bpf(0x06, 0, 0, 0x80000000)]
    prog += [_bpf(0x15, len(deny) - i, 0, nr) for i, nr in enumerate(deny)]
    prog += [_bpf(0x06, 0, 0, 0x7FFF0000), _bpf(0x06, 0, 0, 0x00050000 | errno.EPERM)]
    return b"".join(prog)


def install_filter(libc) -> None:
    """no_new_privs, then the filter; raises OSError when the kernel refuses either."""
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set no_new_privs")
    code = seccomp_program()
    buf = ctypes.create_string_buffer(code, len(code))

    class Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
    prog = Fprog(len(code) // 8, ctypes.cast(buf, ctypes.c_void_p))
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp filter not installed")


def filter_self_checks(libc):
    """One call of each class the filter closes, with the result each gave: a user namespace,
    an exec of a path that does not exist, a SysV segment, a session key. Each must be -1
    with EPERM."""
    for name, call in (("unshare", lambda: libc.unshare(CLONE_NEWUSER)),
                       ("execve", lambda: libc.execve(b"/nonexistent", None, None)),
                       ("shmget", lambda: libc.shmget(0, 4096, 0o1000 | 0o600)),
                       ("add_key", lambda: libc.syscall(248, b"user", b"x", b"x", 1, -3))):
        rc = call()
        yield name, rc, ctypes.get_errno()


def check_box(casa_pid: int, label: str, uid: int) -> None:
    """Every refusal the box relies on, proven on this process before the script runs."""
    libc = ctypes.CDLL(None, use_errno=True)
    # Casa's death kills the run; set after the uid change (the kernel clears it with the
    # credentials), and the parent re-checked right after, since Casa may have died already.
    if libc.prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0:
        refuse(PARENT, "cannot arm the parent-death signal")
    if os.getppid() != casa_pid:
        refuse(PARENT, f"parent is {os.getppid()}, expected {casa_pid}")
    ids = (*os.getresuid(), *os.getresgid())
    if ids != (uid,) * 6 or os.getgroups():
        refuse(IDENTITY, f"ids are {ids} groups {os.getgroups()}, expected uid {uid}")
    # Capabilities: effective, permitted and inheritable must all be empty (capget(2) v3).
    hdr = (ctypes.c_uint32 * 2)(0x20080522, 0)
    data = (ctypes.c_uint32 * 6)()
    if libc.capget(hdr, data) != 0 or any(data):
        refuse(IDENTITY, f"capabilities are not empty: {list(data)}")
    if not set(os.environ) <= {"LC_CTYPE"}:   # CPython's C-locale coercion writes it, nothing else
        refuse(IDENTITY, f"environment is not empty: {sorted(os.environ)}")
    got = None
    last = None
    for path in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            with open(path, "rb") as fh:
                got = fh.read().rstrip(b"\0\n").decode("ascii", "replace")
            break
        except OSError as exc:
            got = None
            last = exc
    if got is None:
        refuse(LABEL, f"cannot read my confinement label ({last.strerror})")
    if got != label:
        refuse(LABEL, f"confinement is {got!r}, expected {label!r}")
    try:
        with open(SENTINEL, "rb"):
            pass
    except OSError as exc:
        refuse(GENERATION, f"the loaded child profile is not this text's ({exc.strerror})")
    for family, name in ((socket.AF_UNIX, "unix"), (socket.AF_INET, "inet")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except PermissionError:
            continue
        except OSError as exc:
            refuse(SOCKET, f"{name} socket failed with {exc.strerror}, not a refusal")
        sock.close()
        refuse(SOCKET, f"a {name} socket opened: the profile is not enforced")
    for path in MUST_REFUSE_READ:
        try:
            with open(path, "rb"):
                pass
        except PermissionError:
            continue
        except OSError as exc:
            refuse(FILE, f"{path}: {exc.strerror}, so nothing is proven")
        refuse(FILE, f"{path} opened: the profile is not enforced")
    # Descriptors: an open of the fd directory must land on 3, the lowest free number, so
    # 0, 1 and 2 are all open (a closed stdin with a leaked 3 would land on 0); closed again,
    # the listing of /proc/self/fd must be 0, 1, 2 and the listing's own fd, 3, so nothing
    # else is open. (An fstat sweep to RLIMIT_NOFILE costs 0.6 s at Docker's default.)
    try:
        d = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY)
        os.close(d)
        fds = sorted(int(name) for name in os.listdir("/proc/self/fd"))
    except OSError as exc:
        refuse(FDS, f"cannot list my descriptors ({exc.strerror})")
    if d != 3 or fds != [0, 1, 2, 3]:
        refuse(FDS, f"descriptors open: {fds} (first free fd {d})")
    try:
        if os.nice(NICE) != NICE:
            raise OSError("priority did not move")
    except OSError as exc:
        refuse(LIMIT, f"nice: {exc}")
    for name, limit in LIMITS:
        try:
            resource.setrlimit(getattr(resource, name), limit)
        except (OSError, ValueError) as exc:
            refuse(LIMIT, f"{name}: {exc}")
    try:
        install_filter(libc)
    except OSError as exc:
        refuse(FILTER, f"{exc.strerror}: {os.strerror(exc.errno)}")
    # The filter is observed, not assumed.
    for name, rc, err in filter_self_checks(libc):
        if rc != -1:
            refuse(FILTER, f"{name} succeeded: the syscall filter is not in place")
        if err != errno.EPERM:
            refuse(FILTER, f"{name} failed with {os.strerror(err)}, not EPERM")


def read_payload() -> tuple[str, list]:
    try:
        payload = json.loads(sys.stdin.buffer.read())
        script, sections = payload["script"], payload["sections"]
        if not isinstance(script, str) or not all(
                isinstance(n, str) and isinstance(t, str) for n, t in sections):
            raise ValueError("shape")
    except Exception as exc:  # noqa: BLE001 — Casa's bug; after the frame, so the script's 1
        sys.stderr.write(f"box: stdin is not a run payload ({type(exc).__name__})\n")
        sys.stderr.flush()
        os._exit(1)
    return script, sections


def run_script(script: str, sections: list) -> tuple[int, dict]:
    """Runs *script* over ``pad``; returns the exit status and the result document. The result
    carries the pad only when the status is 0 and the script's final ``pad`` binding is a dict
    of name to text (read from the globals, so ``pad = {...}`` counts as much as a mutation;
    its items are taken once, so a mapping whose items change between calls yields its
    first answer). This is the child's report to the script; Casa checks the shape again."""
    pad = {name: text for name, text in sections}
    glb = {"__name__": "__main__", "pad": pad}
    capture = _Capture()
    real, sys.stdout = sys.stdout, capture
    status = 0
    try:
        exec(compile(script, "<run>", "exec"), glb)
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            status = 0
        elif isinstance(code, int):
            status = code if 0 < code < 100 else 1
        else:
            sys.stderr.write(f"{code}\n")
            status = 1
    except BaseException:  # noqa: BLE001 — the script's own error, reported as a traceback
        traceback.print_exc(file=sys.stderr)
        status = 1
    finally:
        sys.stdout = real
    result = {"printed": capture.text, "printed_total": capture.total, "pad": None}
    if status == 0:
        final = glb.get("pad")
        items = list(final.items()) if isinstance(final, dict) else None
        if items is not None and all(isinstance(k, str) and isinstance(v, str) for k, v in items):
            result["pad"] = [[k, v] for k, v in items]
        else:
            sys.stderr.write("pad must end as a dict of section name to text\n")
            status = 1
    return status, result


def write_result(out, result: dict) -> None:
    """The result as stdout's last line, after a newline that ends whatever the script wrote
    to the real stdout; ASCII only, so a lone surrogate travels as its escape."""
    out.write("\n" + json.dumps(result, ensure_ascii=True) + "\n")
    out.flush()


def main() -> None:
    try:
        casa_pid, label, uid = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
        check_box(casa_pid, label, uid)
    except BaseException as exc:  # noqa: BLE001 — anything else before the frame is the box's
        refuse(UNEXPECTED, f"{type(exc).__name__}: {exc}")
    out = sys.__stdout__
    out.write('{"box": "ready"}\n')      # from here on every exit status is the script's
    out.flush()
    script, sections = read_payload()
    status, result = run_script(script, sections)
    write_result(out, result)
    sys.stderr.flush()
    os._exit(status)


if __name__ == "__main__":
    main()
