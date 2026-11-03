"""Shared fixtures for the memory wipe's tests: a stand-in root holding a
canary in every registry row, the items a wipe keeps, a stand-in ``/proc``
naming the container start, and the container-start step run as a real
process that can be killed at a chosen point.

Every process killed here is one these helpers spawned; a kill is always the
child signalling itself."""
from __future__ import annotations

import hashlib
import json
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

EID_OPEN = "0a" * 16          # an open, release-provisioned engagement
EID_DONE = "0b" * 16          # completed, still retained
EID_EXPIRED = "0c" * 16       # completed, its retention passed
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


def _defn(tmp: Path, template: str):
    import types
    prompt = write(tmp / "prompt.md", template)
    return types.SimpleNamespace(
        type="x", prompt_template_path=str(prompt), hooks_path=None,
        hooks_document={}, tools_allowed=[], permission_mode="acceptEdits",
        doctrine_dir="", model="", memory=types.SimpleNamespace(enabled=False))


TEMPLATE = ("# Task\n{task}\n\n## Context\n{context}\n\n{executor_memory}\n\n"
            "## Rules\nKeep going.\n")


def memory_block(canary: str) -> str:
    """An executor-memory block holding *canary*."""
    return (f"## Prior engagements (lessons learned)\n- remembered {canary}\n")


async def provision(root: Path, eid: str, *, memory: str, monkeypatch,
                    task: str = "Tidy the garden notes",
                    template: str = TEMPLATE, context: str = "ctx") -> Path:
    """This release's ``provision_workspace`` on the stand-in root."""
    from drivers import workspace
    monkeypatch.setattr(workspace, "CONTROL_ROOT",
                        str(under(root, "/data/engagement-ctl")))
    defn = _defn(root / "defs" / eid, template)
    path = await workspace.provision_workspace(
        engagements_root=str(under(root, "/data/engagements")),
        engagement_id=eid, engagement_auth_token="tok-" + eid[:6],
        defn=defn, task=task, context=context, executor_memory=memory,
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework")
    return Path(path)


def marked_workspace(root: Path, eid: str, block: str, *,
                     status: str = "COMPLETED",
                     retention_until: str = "2999-01-01T00:00:00Z",
                     text: str | None = None) -> Path:
    """A workspace as this release's writer leaves it: ``CLAUDE.md`` with the
    block between its markers (or *text* as given), the cache beside it, and
    the sweep's facts in the control directory."""
    from memory import executor_block
    ws = under(root, f"/data/engagements/{eid}")
    write(ws / "CLAUDE.md", text if text is not None else executor_block.render(
        TEMPLATE, {"{task}": "Old task", "{context}": "(none)"}, block))
    ctl = under(root, f"/data/engagement-ctl/{eid}")
    write(ctl / ".executor_memory", block)
    write(ctl / ".casa-meta.json", json.dumps({
        "engagement_id": eid, "status": status,
        "retention_until": retention_until if status != "UNDERGOING" else None,
    }))
    return ws


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


# --- provisioning in a child process ----------------------------------------

def provision_in_child(root: Path, eid: str, *, memory: str, hook: str = ""
                       ) -> subprocess.CompletedProcess:
    """``provision_workspace`` in its own process on the stand-in root.
    *hook* runs first and may arrange a kill."""
    load = "from drivers import workspace as ws"
    defs = root / "defs" / eid
    body = "\n".join([
        "import asyncio, types, pathlib",
        load,
        f"ws.CONTROL_ROOT = {str(under(root, '/data/engagement-ctl'))!r}",
        f"pathlib.Path({str(defs)!r}).mkdir(parents=True, exist_ok=True)",
        f"p = pathlib.Path({str(defs / 'prompt.md')!r}); p.write_text({TEMPLATE!r})",
        "defn = types.SimpleNamespace(type='x', prompt_template_path=str(p),"
        " hooks_path=None, hooks_document={}, tools_allowed=[],"
        " permission_mode='acceptEdits', doctrine_dir='', model='',"
        " memory=types.SimpleNamespace(enabled=False))",
        hook,
        "asyncio.run(ws.provision_workspace(",
        f"    engagements_root={str(under(root, '/data/engagements'))!r},",
        f"    engagement_id={eid!r}, engagement_auth_token='tok',",
        f"    defn=defn, task='Old task', context='(none)', executor_memory={memory!r},",
        "    casa_framework_mcp_url='http://127.0.0.1:8100/mcp/casa-framework'))",
        "print('completed')",
    ])
    (under(root, "/data/engagements")).mkdir(parents=True, exist_ok=True)
    return run_child(body)


def kill_at_write(k: int) -> str:
    """Die just before the *k*-th file write or rename the process issues."""
    return f"""
import sys as _sys
_n = {{"n": 0}}
def _audit(event, args):
    if event == 'open' and len(args) > 2 and isinstance(args[2], int) and args[2] & 3:
        hit = True
    elif event in ('os.rename', 'os.replace'):
        hit = True
    else:
        return
    _n['n'] += 1
    if _n['n'] == {k}:
        _die()
_sys.addaudithook(_audit)
"""
