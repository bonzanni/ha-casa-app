"""Shared fixtures for the memory wipe's tests: a stand-in root holding a
canary in every registry row, the items a wipe keeps, a stand-in ``/proc``
naming the container start, and the container-start step run as a real
process that can be killed at a chosen point.

Every process killed here is one these helpers spawned; a kill is always the
child signalling itself."""
from __future__ import annotations

import hashlib
import os
import secrets
import signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CODE_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"

SID = "11111111-2222-4333-8444-555555555555"


def under(root: Path, declared: str) -> Path:
    return root / declared.lstrip("/")


def write(path: Path, data: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    return path


def fake_proc(root: Path, *, start: int = 4242,
              boot_id: str = "c0ffee00-0000-4000-8000-000000000001") -> Path:
    """A stand-in ``/proc`` whose PID 1 started at *start* (a new container
    start is a new *start*)."""
    proc = root / "proc"
    write(proc / "sys/kernel/random/boot_id", boot_id + "\n")
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 20
    write(proc / "1/stat", "1 (s6-svscan) " + " ".join(fields) + "\n")
    return proc


def container_id_of(proc: Path) -> str:
    sys.path.insert(0, str(CODE_ROOT))
    from memory import maintenance
    return maintenance.container_id(str(proc))


@dataclass
class Tree:
    root: Path
    canaries: list[str] = field(default_factory=list)
    #: path → bytes of every item the wipe must keep
    kept: dict[Path, bytes] = field(default_factory=dict)

    def canary(self) -> str:
        c = secrets.token_hex(12)
        self.canaries.append(c)
        return c

    def keep(self, path: Path, data: str | bytes) -> Path:
        write(path, data)
        self.kept[path] = path.read_bytes()
        return path


def hits(root: Path, canaries: list[str]) -> list[str]:
    """Every file under *root* holding any canary's bytes."""
    needles = [c.encode() for c in canaries]
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():     # a FIFO never ends
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if any(n in data for n in needles):
                found.append(str(p.relative_to(root)))
    return sorted(found)


def kept_changes(tree: Tree) -> list[str]:
    return sorted(str(p) for p, data in tree.kept.items()
                  if not p.exists() or p.read_bytes() != data)


# --- a real process, killable at a chosen point -------------------------------

KILL_PRELUDE = """
import os, signal, sys
sys.path.insert(0, {code!r})
_calls = {{"n": 0}}
def _die():
    os.kill(os.getpid(), signal.SIGKILL)
"""


def run_child(body: str, *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run *body* in a fresh interpreter with the code root importable."""
    script = KILL_PRELUDE.format(code=str(CODE_ROOT)) + textwrap.dedent(body)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, env=env, cwd="/", timeout=timeout)


def killed(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == -signal.SIGKILL


def run_oneshot(root: Path, proc: Path, *, kill_at: str | None = None,
                kill_after: int = 1, now: float | None = None
                ) -> subprocess.CompletedProcess:
    """``memory.maintenance.run`` in its own process. *kill_at* names a
    module function of ``memory.maintenance``; the process SIGKILLs itself
    right after its *kill_after*-th call returns."""
    lines = ["from memory import maintenance as m"]
    if kill_at is not None:
        lines += [
            f"_real = getattr(m, {kill_at!r})",
            "def _wrapped(*a, **k):",
            "    out = _real(*a, **k)",
            "    _calls['n'] += 1",
            f"    if _calls['n'] >= {kill_after}:",
            "        _die()",
            "    return out",
            f"setattr(m, {kill_at!r}, _wrapped)",
        ]
    lines.append(f"print(m.run(root={str(root)!r}, proc_root={str(proc)!r}, "
                 f"now={now!r}))")
    return run_child("\n".join(lines))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
