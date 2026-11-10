"""The script runner's prelude (``casa/rootfs/opt/casa/run/child.py``), on a developer's
machine: no AppArmor here, so a green run proves the plumbing, the encodings, the filter's
bytes and the fail-closed paths, never the confinement. Everything that changes a process
(a refusal's ``os._exit``, an rlimit, the syscall filter) runs in a forked child of this
test's own, and nothing here signals or inspects any other process.
"""
from __future__ import annotations

import ctypes
import errno
import importlib.util
import io
import json
import os
import resource
import select
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHILD = REPO / "casa" / "rootfs" / "opt" / "casa" / "run" / "child.py"
PROFILE = REPO / "casa" / "apparmor.txt"
DIGEST_SCRIPT = REPO / "casa" / "rootfs" / "opt" / "casa" / "scripts" / "pyrun_digest.py"

_spec = importlib.util.spec_from_file_location("child", CHILD)
child = importlib.util.module_from_spec(_spec)
sys.dont_write_bytecode = True      # the prelude's directory in the tree stays as shipped
_spec.loader.exec_module(child)
sys.dont_write_bytecode = False

LABEL = "local_casa-test//pyrun (enforce)"
EPERM = errno.EPERM


# --- running in a forked child ------------------------------------------------------------


def _in_child(fn, *, keep_fds: tuple[int, ...] = (), close_stdin: bool = False):
    """Fork; in the child, stdout and stderr are pipes back here, every other descriptor
    above 2 is closed (but *keep_fds*), stdin is /dev/null (closed when *close_stdin*), then
    *fn* runs and the child exits 0, or with what *fn*'s ``os._exit`` says. Returns
    (exit status or -signal, stdout bytes, stderr text)."""
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            null = os.open("/dev/null", os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(out_w, 1)
            os.dup2(err_w, 2)
            # The kept descriptors move to 3, 4, ...: a leak is a leak at the lowest numbers.
            kept = [os.dup(fd) for fd in keep_fds]
            for fd in range(3, 4096):
                if fd not in kept:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            for i, fd in enumerate(kept):
                if fd != 3 + i:
                    os.dup2(fd, 3 + i)
                    os.close(fd)
            # Plain fd-backed streams, as the real box has: pytest's captures lost their
            # files when every other descriptor closed above.
            sys.stdin = sys.__stdin__ = io.TextIOWrapper(io.FileIO(0, "r", closefd=False))
            sys.stdout = sys.__stdout__ = io.TextIOWrapper(io.FileIO(1, "w", closefd=False),
                                                           encoding="utf-8", write_through=True)
            sys.stderr = sys.__stderr__ = io.TextIOWrapper(io.FileIO(2, "w", closefd=False),
                                                           encoding="utf-8", write_through=True)
            if close_stdin:
                os.close(0)
            fn()
        except BaseException:  # noqa: BLE001 — the child reports and dies, whatever happened
            import traceback
            traceback.print_exc(file=sys.__stderr__)
            sys.__stderr__.flush()
            os._exit(250)
        sys.__stdout__.flush()
        sys.__stderr__.flush()
        os._exit(0)
    os.close(out_w)
    os.close(err_w)
    chunks = {out_r: [], err_r: []}
    open_fds = {out_r, err_r}
    while open_fds:
        ready, _, _ = select.select(list(open_fds), [], [])
        for fd in ready:
            data = os.read(fd, 65536)
            if data:
                chunks[fd].append(data)
            else:
                open_fds.discard(fd)
                os.close(fd)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code, b"".join(chunks[out_r]), b"".join(chunks[err_r]).decode("utf-8", "replace")


def _passing_box(label: str = LABEL, *, sentinel_error=None, files_open=False,
                 sockets=None, label_text=None, label_unreadable=False, groups=(),
                 environ=None, limits=None, nice=None, install=None, self_checks=None,
                 capget_nonzero=False, listdir=None):
    """Patches, applied inside the forked child, that let ``check_box`` pass here: the label
    and the sentinel come from injected opens, the two sockets and the two files refuse.
    Each keyword breaks one of them."""
    def apply():
        real_open = open
        label_paths = {"/proc/self/attr/apparmor/current", "/proc/self/attr/current"}

        def fake_open(path, *args, **kwargs):
            if path in label_paths:
                if label_unreadable:
                    raise OSError(errno.EINVAL, "Invalid argument")
                return io.BytesIO((label_text if label_text is not None else label).encode() + b"\n")
            if path == child.SENTINEL:
                if sentinel_error is not None:
                    raise sentinel_error
                return io.BytesIO(b"")
            if path in child.MUST_REFUSE_READ and not files_open:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *args, **kwargs)
        child.open = fake_open

        def refusing_socket(family, kind):
            if sockets is not None:
                return sockets(family, kind)
            raise PermissionError(errno.EACCES, "Permission denied")
        child.socket.socket = refusing_socket
        os.getgroups = lambda: list(groups)
        os.environ.clear()
        if environ:
            os.environ.update(environ)
        if limits is not None:
            child.LIMITS = limits
        if nice is not None:
            os.nice = nice
        if install is not None:
            child.install_filter = install
        if self_checks is not None:
            child.filter_self_checks = self_checks
        if listdir is not None:
            os.listdir = listdir
        if capget_nonzero:
            real = ctypes.CDLL(None, use_errno=True)

            class Libc:
                def __getattr__(self, name):
                    return getattr(real, name)

                @staticmethod
                def capget(hdr, data):
                    data[0] = 1
                    return 0
            child.ctypes.CDLL = lambda *a, **k: Libc()
    return apply


def _run_box(patches, *, uid=None, casa_pid=None, label=LABEL, after=None, **child_kw):
    """``check_box`` in a forked child with *patches*; *after* runs when it returned."""
    def body():
        patches()
        child.check_box(casa_pid if casa_pid is not None else os.getppid(), label,
                        os.getuid() if uid is None else uid)
        if after is not None:
            after()
    return _in_child(body, **child_kw)


@pytest.fixture(autouse=True)
def _nice_zero():
    if os.nice(0) != 0:
        pytest.skip("the prelude requires nice to start at 0")


# --- end to end here: it refuses ------------------------------------------------------------


def test_the_prelude_refuses_on_this_machine_before_running_anything():
    """No AppArmor here, so the real prelude as a subprocess must refuse: its identity check
    (a developer has groups) or its label check, with no ready frame and nothing on stdout.
    Red case: a prelude with its checks removed prints RAN here."""
    payload = json.dumps({"script": "print('RAN')", "sections": [["", ""], ["a", "x"]]})
    done = subprocess.run([sys.executable, "-I", "-S", "-B", str(CHILD), str(os.getpid()),
                           LABEL, str(os.getuid())], input=payload.encode(),
                          capture_output=True, env={}, close_fds=True)
    assert done.returncode in (child.IDENTITY, child.LABEL), done.stderr
    assert done.stdout == b""
    assert done.stderr.startswith(b"box: ")
    assert done.stderr.count(b"\n") == 1


def test_the_tree_carries_one_digest():
    """The sentinel rule in casa/apparmor.txt, the text's digest and PROFILE_DIGEST agree.
    Red case: edit a child-profile line without regenerating, and this fails."""
    out = subprocess.run([sys.executable, str(DIGEST_SCRIPT), "check", str(PROFILE), str(CHILD)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert child.PROFILE_DIGEST in out.stdout
    assert child.SENTINEL == f"/opt/casa/run/pyrun.{child.PROFILE_DIGEST}"
    assert f"/opt/casa/run/pyrun.{child.PROFILE_DIGEST} r," in PROFILE.read_text()


# --- check_box, piece by piece, in a forked child -----------------------------------------


def test_every_check_passing_leaves_the_box_as_described():
    def after():
        limits = {name: resource.getrlimit(getattr(resource, name)) for name, _ in child.LIMITS}
        status = open("/proc/self/status").read()
        seccomp = status.split("Seccomp:")[1].split()[0]
        nnp = status.split("NoNewPrivs:")[1].split()[0]
        sys.__stdout__.write(json.dumps({"limits": limits, "nice": os.nice(0),
                                         "seccomp": seccomp, "nnp": nnp}))
    code, out, err = _run_box(_passing_box(), after=after)
    assert (code, err) == (0, "")
    seen = json.loads(out)
    assert seen["limits"] == {name: list(limit) for name, limit in child.LIMITS}
    assert seen["nice"] == 10
    assert (seen["seccomp"], seen["nnp"]) == ("2", "1")


def test_a_parent_that_is_not_casa_refuses():
    code, out, err = _run_box(_passing_box(), casa_pid=1)
    assert (code, out) == (child.PARENT, b"")
    assert err.startswith("box: parent is ")


def test_an_identity_that_is_not_the_slots_refuses():
    code, _, err = _run_box(_passing_box(), uid=os.getuid() + 1)
    assert code == child.IDENTITY and "ids are" in err
    code, _, err = _run_box(_passing_box(groups=(4, 20)))
    assert code == child.IDENTITY and "groups [4, 20]" in err
    code, _, err = _run_box(_passing_box(capget_nonzero=True))
    assert code == child.IDENTITY and "capabilities are not empty" in err


def test_the_environment_may_hold_only_the_coerced_locale():
    code, _, _ = _run_box(_passing_box(environ={"LC_CTYPE": "C.UTF-8"}))
    assert code == 0
    code, _, err = _run_box(_passing_box(environ={"LC_CTYPE": "C.UTF-8", "HOME": "/root"}))
    assert code == child.IDENTITY and "environment is not empty: ['HOME', 'LC_CTYPE']" in err
    code, _, err = _run_box(_passing_box(environ={"CLAUDE_CODE_OAUTH_TOKEN": "x"}))
    assert code == child.IDENTITY and "CLAUDE_CODE_OAUTH_TOKEN" in err


def test_the_label_must_be_the_enforced_child_label():
    code, _, err = _run_box(_passing_box(label_unreadable=True))
    assert code == child.LABEL and "cannot read my confinement label" in err
    for got in ("local_casa-test (enforce)", "local_casa-test//pyrun (complain)", "unconfined",
                "local_casa-test//pyrun", "other_casa//pyrun (enforce)"):
        code, _, err = _run_box(_passing_box(label_text=got))
        assert code == child.LABEL, got
        assert f"confinement is {got!r}" in err


def test_the_label_is_read_from_the_second_path_when_the_first_is_missing():
    """A kernel without the apparmor/ subdirectory answers at attr/current."""
    def patches():
        _passing_box()()
        inner = child.open

        def only_legacy(path, *args, **kwargs):
            if path == "/proc/self/attr/apparmor/current":
                raise FileNotFoundError(errno.ENOENT, "No such file or directory")
            return inner(path, *args, **kwargs)
        child.open = only_legacy
    code, _, err = _run_box(patches)
    assert (code, err) == (0, "")


def test_a_sentinel_that_does_not_open_refuses():
    """The loaded child profile is of another text: its rule names another file."""
    for exc in (PermissionError(errno.EACCES, "Permission denied"),
                FileNotFoundError(errno.ENOENT, "No such file or directory")):
        code, _, err = _run_box(_passing_box(sentinel_error=exc))
        assert code == child.GENERATION, exc
        assert "the loaded child profile is not this text's" in err


def test_a_socket_that_opens_or_fails_otherwise_refuses():
    import socket as socket_module
    real_socket = socket_module.socket
    code, _, err = _run_box(_passing_box(sockets=real_socket))
    assert code == child.SOCKET and "a unix socket opened" in err

    def unix_refused_inet_open(family, kind):
        if family == socket_module.AF_UNIX:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_socket(family, kind)
    code, _, err = _run_box(_passing_box(sockets=unix_refused_inet_open))
    assert code == child.SOCKET and "a inet socket opened" in err

    def other_error(family, kind):
        raise OSError(errno.EAFNOSUPPORT, "Address family not supported")
    code, _, err = _run_box(_passing_box(sockets=other_error))
    assert code == child.SOCKET and "not a refusal" in err


def test_a_world_readable_file_that_opens_refuses():
    code, _, err = _run_box(_passing_box(files_open=True))
    assert code == child.FILE and "/etc/passwd opened" in err

    def patches():
        _passing_box()()
        inner = child.open

        def missing(path, *args, **kwargs):
            if path == "/proc/1/cmdline":
                raise FileNotFoundError(errno.ENOENT, "No such file or directory")
            return inner(path, *args, **kwargs)
        child.open = missing
    code, _, err = _run_box(patches)
    assert code == child.FILE and "/proc/1/cmdline: No such file or directory, so nothing is proven" in err


def test_the_descriptor_check_takes_its_exact_form():
    """Exactly 0, 1 and 2 open: the directory opens at 3 and, closed again, the listing is
    0, 1, 2 and the listing's own 3. One leaked descriptor lists five; a closed stdin with a
    leaked 3 lands the open on 0, which the listing alone would not show."""
    leaked = os.open("/dev/null", os.O_RDONLY)
    try:
        code, _, err = _run_box(_passing_box(), keep_fds=(leaked,))
        assert code == child.FDS
        assert err == "box: descriptors open: [0, 1, 2, 3, 4] (first free fd 4)\n"
        code, _, err = _run_box(_passing_box(), keep_fds=(leaked,), close_stdin=True)
        assert code == child.FDS
        assert err == "box: descriptors open: [0, 1, 2, 3] (first free fd 0)\n"
    finally:
        os.close(leaked)

    def unlistable(path):
        raise PermissionError(errno.EACCES, "Permission denied")
    code, _, err = _run_box(_passing_box(listdir=unlistable))
    assert code == child.FDS and "cannot list my descriptors" in err


def test_a_priority_that_does_not_move_refuses():
    code, _, err = _run_box(_passing_box(nice=lambda n: 0))
    assert code == child.LIMIT and "nice: priority did not move" in err


def test_a_limit_the_kernel_refuses_refuses():
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    code, _, err = _run_box(_passing_box(limits=(("RLIMIT_NOFILE", (hard + 1, hard + 1)),)))
    assert code == child.LIMIT and "RLIMIT_NOFILE:" in err


def test_a_filter_that_does_not_install_or_does_not_refuse_refuses():
    def no_install(libc):
        raise OSError(errno.EINVAL, "seccomp filter not installed")
    code, _, err = _run_box(_passing_box(install=no_install))
    assert code == child.FILTER and "seccomp filter not installed: Invalid argument" in err

    def one_succeeds(libc):
        yield "shmget", 5, 0
    code, _, err = _run_box(_passing_box(self_checks=one_succeeds))
    assert code == child.FILTER and "shmget succeeded: the syscall filter is not in place" in err

    def one_other_errno(libc):
        yield "execve", -1, errno.ENOENT
    code, _, err = _run_box(_passing_box(self_checks=one_other_errno))
    assert code == child.FILTER and "execve failed with No such file or directory, not EPERM" in err


def test_anything_unexpected_before_the_frame_is_a_box_line_with_exit_107():
    def bad_argv():
        sys.argv = ["child.py", "notanint", LABEL, "1"]
        child.main()
    code, out, err = _in_child(bad_argv)
    assert (code, out) == (child.UNEXPECTED, b"")
    assert err.startswith("box: ValueError: ")

    def raising_check():
        def boom(*a):
            raise TypeError("unexpected")
        child.check_box = boom
        sys.argv = ["child.py", str(os.getppid()), LABEL, str(os.getuid())]
        child.main()
    code, out, err = _in_child(raising_check)
    assert (code, out, err) == (child.UNEXPECTED, b"", "box: TypeError: unexpected\n")


def test_main_writes_the_frame_then_the_result_and_exits_with_the_scripts_status():
    """Through main() with the checks passing here: the frame is stdout's first line, the
    result its last; a bad payload after the frame is the script's 1, never a refusal."""
    def run(payload: bytes):
        def body():
            _passing_box()()
            r, w = os.pipe()
            os.write(w, payload)
            os.close(w)
            os.dup2(r, 0)
            os.close(r)
            sys.argv = ["child.py", str(os.getppid()), LABEL, str(os.getuid())]
            child.main()
        return _in_child(body)
    payload = json.dumps({"script": "print('hi'); pad['b'] = 'y\\n'", "sections": [["", ""], ["a", "x\n"]]})
    code, out, err = run(payload.encode())
    lines = out.split(b"\n")
    assert code == 0 and err == ""
    assert lines[0] == b'{"box": "ready"}'
    assert json.loads(lines[-2]) == {"printed": "hi\n", "printed_total": 3,
                                     "pad": [["", ""], ["a", "x\n"], ["b", "y\n"]]}
    code, out, err = run(json.dumps({"script": "import sys; sys.exit(7)", "sections": []}).encode())
    assert code == 7 and out.split(b"\n")[0] == b'{"box": "ready"}'
    assert json.loads(out.split(b"\n")[-2])["pad"] is None
    code, out, err = run(b"not json")
    assert code == 1 and out == b'{"box": "ready"}\n'
    assert err == "box: stdin is not a run payload (JSONDecodeError)\n"
    code, out, err = run(json.dumps({"script": "pass", "sections": [[1, 2]]}).encode())
    assert code == 1 and err == "box: stdin is not a run payload (ValueError)\n"


# --- the limits and the filter, observed in a forked child --------------------------------


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _err(rc) -> str:
    return "ok" if rc != -1 else errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))


def test_nice_and_rlimit_nice_hold_the_priority():
    def body():
        resource.setrlimit(resource.RLIMIT_NICE, (0, 0))
        resource.setrlimit(resource.RLIMIT_RTPRIO, (0, 0))
        moved = os.nice(child.NICE)
        try:
            os.setpriority(os.PRIO_PROCESS, 0, 0)
            back = "worked"
        except PermissionError as exc:
            back = errno.errorcode[exc.errno]
        sys.__stdout__.write(json.dumps([moved, back, os.nice(0)]))
    code, out, _ = _in_child(body)
    assert code == 0 and json.loads(out) == [10, "EACCES", 10]


def test_the_rlimits_bind_what_they_say():
    def body():
        libc = _libc()
        for name, limit in child.LIMITS:
            resource.setrlimit(getattr(resource, name), limit)
        seen = {}
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        fd = os.open("/dev/shm" if os.path.isdir("/dev/shm") else "/tmp", os.O_TMPFILE | os.O_RDWR, 0o600)
        try:
            os.write(fd, b"x")
            seen["file write under FSIZE 0"] = "worked"
        except OSError as exc:
            seen["file write under FSIZE 0"] = errno.errorcode[exc.errno]
        os.close(fd)
        seen["pipe write under FSIZE 0"] = os.write(1, b"") == 0 and "worked" or "?"
        seen["mlock under MEMLOCK 0"] = _err(libc.mlock(ctypes.create_string_buffer(4096), 4096))
        try:
            bytearray(600 * child.MiB)
            seen["600 MiB under AS 512 MiB"] = "allocated"
        except MemoryError:
            seen["600 MiB under AS 512 MiB"] = "MemoryError"
        # A test worker's address space is already partly used; what fits is half the room
        # left under the limit, at most 100 MiB (a fresh box has room for far more).
        used = int(next(line for line in open("/proc/self/status") if line.startswith("VmSize:")).split()[1]) * 1024
        bytearray(max(1, min(100 * child.MiB, (512 * child.MiB - used) // 2)))
        seen["an allocation that fits under AS 512 MiB"] = "allocated"
        rc = libc.fork()
        if rc == 0:
            os._exit(0)
        seen["fork under NPROC 1"] = _err(rc)
        seen["NOFILE"] = resource.getrlimit(resource.RLIMIT_NOFILE)
        sys.__stdout__.write(json.dumps(seen))
    code, out, err = _in_child(body)
    assert code == 0, err
    assert json.loads(out) == {
        "file write under FSIZE 0": "EFBIG", "pipe write under FSIZE 0": "worked",
        "mlock under MEMLOCK 0": "EPERM", "600 MiB under AS 512 MiB": "MemoryError",
        "an allocation that fits under AS 512 MiB": "allocated", "fork under NPROC 1": "EAGAIN",
        "NOFILE": [32, 32]}


def test_the_cpu_limit_stops_a_busy_loop():
    def body():
        resource.setrlimit(resource.RLIMIT_CPU, (1, 2))
        while True:
            pass
    code, _, _ = _in_child(body)
    assert code == -signal.SIGXCPU


def _decode(program: bytes):
    ins = [struct.unpack("HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]

    def run(arch, nr):
        pc, acc = 0, 0
        while True:
            code, jt, jf, k = ins[pc]
            if code == 0x20:
                acc = arch if k == 4 else nr
                pc += 1
            elif code == 0x15:
                pc += 1 + (jt if acc == k else jf)
            elif code == 0x35:
                pc += 1 + (jt if acc >= k else jf)
            elif code == 0x06:
                return k
            else:
                raise AssertionError(f"unexpected BPF opcode {code:#x}")
    return run


RET_KILL, RET_ALLOW, RET_EPERM = 0x80000000, 0x7FFF0000, 0x00050000 | EPERM
AUDIT_ARCH_I386, AUDIT_ARCH_AARCH64 = 0x40000003, 0xC00000B7
DENIED_BY_CLASS = {
    "SysV and mqueue": [29, 30, 31, 67, 68, 69, 70, 71, 64, 65, 66, 220, 240, 241, 242, 243, 244, 245],
    "keys": [248, 249, 250],
    "io_uring": [425, 426, 427],
    "namespaces and tasks": [272, 308, 56, 57, 58, 435],
    "exec": [59, 322],
    "mounts": [165, 166, 155, 428, 429, 430, 431, 432, 433, 442, 457, 458, 467, 161, 167, 168],
    "timestamps": [132, 235, 261, 280],
    "other-process reach": [101, 310, 311, 312, 440, 448, 434, 438, 424, 279, 256, 298, 321, 323],
    "notify": [253, 294, 254, 255, 300, 301],
    "handles": [303, 304],
    "anonymous files": [319, 447],
    "aio": [206, 207, 208, 209, 210, 333],
    "administration": [103, 163, 169, 246, 320, 175, 313, 176, 170, 171, 164, 227, 159, 305, 173,
                       172, 153, 134, 212, 179, 443, 317, 460],
}


def test_the_filter_decodes_to_the_table():
    """Every number 0..1023 under x86_64: EPERM for exactly the 105 of the table, ALLOW for
    the rest; KILL for any x32 number and for every other architecture."""
    table = sorted(n for group in DENIED_BY_CLASS.values() for n in group)
    assert len(table) == len(set(table)) == 105
    assert sorted(child.DENIED_SYSCALLS) == table
    run = _decode(child.seccomp_program())
    denied = [n for n in range(1024) if run(child.AUDIT_ARCH_X86_64, n) == RET_EPERM]
    allowed = [n for n in range(1024) if run(child.AUDIT_ARCH_X86_64, n) == RET_ALLOW]
    assert denied == table
    assert len(allowed) == 1024 - 105
    assert all(run(child.AUDIT_ARCH_X86_64, child.X32_BIT + n) == RET_KILL for n in (0, 1, 59, 1023))
    for arch in (AUDIT_ARCH_I386, AUDIT_ARCH_AARCH64, 0):
        assert all(run(arch, n) == RET_KILL for n in (0, 1, 59, 29, 1023))
    assert len(child.seccomp_program()) == 8 * (6 + 105 + 2)


def test_the_filter_refuses_each_class_and_leaves_the_stdlib_working():
    """In a forked child under the filter: the four self-checks and one call of each other
    class are EPERM, while an open, an ioctl, a signal to itself and the stdlib imports a
    script needs all work."""
    def body():
        libc = _libc()
        child.install_filter(libc)
        seen = {name: (rc, errno.errorcode.get(err, str(err)))
                for name, rc, err in child.filter_self_checks(libc)}
        seen["mount"] = _err(libc.mount(b"/etc", b"/tmp", None, 4096, None))
        seen["open_tree"] = _err(libc.syscall(428, -100, b"/etc", 1))
        try:
            os.utime("/tmp", None)
            seen["utime"] = "ok"
        except OSError as exc:
            seen["utime"] = errno.errorcode[exc.errno]
        seen["inotify_init1"] = _err(libc.syscall(294, 0))
        seen["pidfd_open"] = _err(libc.syscall(434, os.getpid(), 0))
        seen["ptrace"] = _err(libc.syscall(101, 0, 0, 0, 0))
        seen["memfd_create"] = _err(libc.syscall(319, b"x", 0))
        seen["syslog"] = _err(libc.syscall(103, 3, ctypes.create_string_buffer(16), 16))
        seen["bpf"] = _err(libc.syscall(321, 0, None, 0))
        seen["io_setup"] = _err(libc.syscall(206, 1, ctypes.byref(ctypes.c_ulong(0))))
        rc = libc.fork()
        if rc == 0:
            os._exit(0)
        seen["fork"] = _err(rc)
        seen["clone3"] = _err(libc.syscall(435, ctypes.byref((ctypes.c_uint64 * 11)()), 88))
        with open(os.__file__, "rb") as fh:
            seen["open"] = "ok" if fh.read(10) else "empty"
        seen["ioctl"] = _err(libc.ioctl(1, 0x5401, ctypes.create_string_buffer(64)))
        seen["kill self"] = _err(libc.kill(os.getpid(), 0))
        seen["getrandom"] = len(os.urandom(4))
        import decimal, hashlib, json as _json, re, secrets, sqlite3, uuid, zoneinfo  # noqa: F401
        seen["imports"] = [hashlib.sha256(b"x").hexdigest()[:4],
                           sqlite3.connect(":memory:").execute("select 1").fetchone()[0],
                           len(str(uuid.uuid4())), str(decimal.Decimal("0.1") + decimal.Decimal("0.2"))]
        sys.__stdout__.write(_json.dumps(seen))
    code, out, err = _in_child(body)
    assert code == 0, err
    seen = json.loads(out)
    assert seen["unshare"] == seen["execve"] == seen["shmget"] == seen["add_key"] == [-1, "EPERM"]
    for name in ("mount", "open_tree", "utime", "inotify_init1", "pidfd_open", "ptrace",
                 "memfd_create", "syslog", "bpf", "io_setup", "fork", "clone3"):
        assert seen[name] == "EPERM", name
    assert seen["open"] == "ok"
    assert seen["ioctl"] == "ENOTTY"
    assert seen["kill self"] == "ok"
    assert seen["getrandom"] == 4
    assert seen["imports"] == ["2d71", 1, 36, "0.3"]


def test_the_filter_kills_an_x32_number():
    def body():
        libc = _libc()
        child.install_filter(libc)
        libc.syscall(child.X32_BIT + 1)
    code, _, _ = _in_child(body)
    assert code == -signal.SIGSYS


def test_without_the_filter_the_denied_calls_would_work_here():
    """Red case for the self-checks: a developer's uid may create a SysV segment and a
    session key, so a passing self-check means the filter, not the host, refused."""
    def body():
        libc = _libc()
        seen = {}
        sid = libc.shmget(0, 4096, 0o1000 | 0o600)
        seen["shmget"] = _err(sid)
        if sid != -1:
            libc.shmctl(sid, 0, None)
        key = libc.syscall(248, b"user", b"casa-run-test", b"x", 1, -3)
        seen["add_key"] = _err(key)
        if key != -1:
            libc.syscall(250, 3, key)     # KEYCTL_UNLINK from the session keyring
        sys.__stdout__.write(json.dumps(seen))
    code, out, err = _in_child(body)
    assert code == 0, err
    assert json.loads(out)["shmget"] == "ok"


def test_task_creation_with_a_user_namespace_is_eagain_under_nproc_1_alone():
    def body():
        libc = _libc()
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        rc = libc.syscall(56, child.CLONE_NEWUSER | signal.SIGCHLD, 0, 0, 0, 0)
        if rc == 0:
            os._exit(0)
        clone = _err(rc)
        rc = libc.syscall(435, ctypes.byref((ctypes.c_uint64 * 11)(child.CLONE_NEWUSER, signal.SIGCHLD)), 88)
        if rc == 0:
            os._exit(0)
        sys.__stdout__.write(json.dumps([clone, _err(rc)]))
    code, out, err = _in_child(body)
    assert code == 0, err
    assert json.loads(out) == ["EAGAIN", "EAGAIN"]


# --- run_script and the result's write path ------------------------------------------------


def _run(script: str, sections: list) -> tuple[int, int, dict | None, str]:
    """``run_script`` then ``write_result`` in a forked child over a pipe, as main() does:
    (status, stray bytes before the result line, the parsed result, stderr)."""
    def body():
        status, result = child.run_script(script, sections)
        out = io.TextIOWrapper(io.FileIO(1, "wb", closefd=False), encoding="utf-8")
        child.write_result(out, result)
        sys.stderr.flush()
        os._exit(status)
    code, out, err = _in_child(body)
    tail = out[out.rfind(b"\n", 0, len(out) - 1) + 1:] if out else b""
    stray = len(out) - len(tail)
    try:
        doc = json.loads(tail)
    except ValueError:
        doc = None
    return code, stray, doc, err


def test_a_script_changes_adds_and_deletes_sections():
    code, stray, doc, _ = _run("pad['b'] = 'new'\npad['a'] = pad['a'].upper()\ndel pad['']",
                               [["", "top"], ["a", "x"]])
    assert (code, stray) == (0, 1)
    assert doc == {"printed": "", "printed_total": 0, "pad": [["a", "X"], ["b", "new"]]}


def test_printed_output_is_kept_to_the_cap_and_counted_in_full():
    code, _, doc, _ = _run("for _ in range(10000): print('z' * 1000)", [["", ""]])
    assert code == 0
    assert len(doc["printed"]) == child.PRINTED_MAX
    assert doc["printed_total"] == 10_010_000
    assert doc["pad"] == [["", ""]]


def test_a_raise_is_status_1_with_the_traceback_and_no_pad():
    code, _, doc, err = _run("1/0", [["", ""]])
    assert code == 1 and doc["pad"] is None
    assert "ZeroDivisionError" in err and 'File "<run>", line 1' in err


def test_sys_exit_is_the_scripts_own_status():
    assert _run("import sys; sys.exit(3)", [["", ""]])[0] == 3
    assert _run("import sys; sys.exit(0)", [["", ""]])[0] == 0
    assert _run("import sys; sys.exit()", [["", ""]])[2]["pad"] == [["", ""]]
    code, _, doc, err = _run("import sys; sys.exit('bad')", [["", ""]])
    assert (code, doc["pad"], err) == (1, None, "bad\n")
    assert _run("import sys; sys.exit(100)", [["", ""]])[0] == 1
    assert _run("import sys; sys.exit(-1)", [["", ""]])[0] == 1


def test_a_pad_that_is_not_names_to_texts_is_status_1():
    for script in ("pad.clear(); pad[1] = 2", "pad['a'] = 2", "pad = None", "del pad",
                   "pad = [('a', 'b')]"):
        code, _, doc, err = _run(script, [["", ""]])
        assert (code, doc["pad"]) == (1, None), script
        assert err == "pad must end as a dict of section name to text\n", script


def test_the_final_binding_of_pad_is_what_comes_back():
    assert _run("pad = {'': '', 'a': 'new'}", [["", ""], ["a", "old"]])[2]["pad"] == [["", ""], ["a", "new"]]
    assert _run("def f():\n    global pad\n    pad = {'a': 'fn'}\nf()", [["", ""]])[2]["pad"] == [["a", "fn"]]
    assert _run("pad['a'] = 'mut'", [["", ""], ["a", "old"]])[2]["pad"] == [["", ""], ["a", "mut"]]
    assert _run("pass", [["", ""], ["a", "old"]])[2]["pad"] == [["", ""], ["a", "old"]]


def test_a_mapping_whose_items_change_between_calls_yields_its_first_answer():
    script = ("class D(dict):\n"
              "    n = 0\n"
              "    def items(self):\n"
              "        D.n += 1\n"
              "        return [('a', 'x\\n')] if D.n == 1 else [(1, 2)]\n"
              "pad = D()")
    code, _, doc, _ = _run(script, [["", ""]])
    assert (code, doc["pad"]) == (0, [["a", "x\n"]])


def test_bytes_a_script_writes_to_the_real_stdout_are_stray_and_the_result_still_parses():
    code, stray, doc, _ = _run("import os; os.write(1, b'stray\\n')", [["", "t"]])
    assert (code, stray) == (0, 7)
    assert doc["pad"] == [["", "t"]]


def test_a_script_that_replaces_stdout_or_exits_in_finally_is_still_read_by_the_frame_rule():
    code, stray, doc, _ = _run("import sys; sys.stdout = sys.__stdout__; print('x')", [["", ""]])
    assert (code, stray, doc["printed"], doc["pad"]) == (0, 3, "", [["", ""]])
    code, _, doc, _ = _run("try:\n    pad['a'] = 'y'\nfinally:\n    import sys; sys.exit(0)", [["", ""]])
    assert (code, doc["pad"]) == (0, [["", ""], ["a", "y"]])


def test_the_stdlib_is_there_and_a_mark_line_in_a_value_stays_a_value():
    code, _, doc, _ = _run("import re, json\npad['n'] = json.dumps(len(re.findall(r'\\w+', pad[''])))",
                           [["", "a b c"]])
    assert doc["pad"] == [["", "a b c"], ["n", "3"]]
    code, _, doc, _ = _run("pad['a'] = '## not a header\\nline'", [["", ""]])
    assert doc["pad"] == [["", ""], ["a", "## not a header\nline"]]


@pytest.mark.parametrize("name,text", [
    ("lone high surrogate", "x\ud800\n"),
    ("lone low surrogate", "x\udc80\n"),
    ("both", "\U00010080\n"),
    ("NUL 1 MiB", "\x00" * (1024 * 1024)),
    ("emoji", "\U0001F600" * 1000),
    ("CJK", "中" * 1000),
])
def test_the_real_write_path_carries_every_text_back_identical(name, text):
    """The payload and the result are ASCII, so a lone surrogate travels as its escape and
    nothing raises after the frame. Red case: ensure_ascii=False raises on the lone high
    surrogate and breaks the decode on the lone low one."""
    payload = json.dumps({"script": "pass", "sections": [["", ""], ["a", text]]}, ensure_ascii=True)
    assert payload.isascii()
    code, stray, doc, err = _run("pass", json.loads(payload)["sections"])
    assert (code, stray, err) == (0, 1, "")
    assert dict(doc["pad"])["a"] == text


def test_the_result_stays_under_the_read_cap_for_a_full_pad():
    """1 MiB of pad: at most 6 bytes of JSON per byte of text (``\\u0000``), plus the printed
    cap, is under the 8 MiB Casa reads."""
    mib = 1024 * 1024
    for text in ("\x00" * mib, "\U0001F600" * (mib // 4), "中" * (mib // 3), "\udcff" * (mib // 3)):
        assert len(text.encode("utf-8", "surrogatepass")) <= mib
        _, result = child.run_script("pass", [["", ""], ["a", text]])
        size = len(json.dumps(result, ensure_ascii=True))
        assert size <= 6 * (mib + child.PRINTED_MAX) + 4096
    _, result = child.run_script("print('\\x00' * 40000)", [["", ""], ["a", "\x00" * mib]])
    assert len(json.dumps(result, ensure_ascii=True)) <= 6 * (mib + child.PRINTED_MAX) + 4096 < 8 * mib
