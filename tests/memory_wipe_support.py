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
EID_DONE = "0b" * 16          # completed, retained, written before the release
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


def legacy_block(canary: str) -> str:
    """A block as the pre-release producer rendered it."""
    return (f"## Prior engagements (lessons learned)\n- remembered {canary}\n")


async def provision(root: Path, eid: str, *, memory: str, monkeypatch,
                    task: str = "Tidy the garden notes") -> Path:
    """This release's ``provision_workspace`` on the stand-in root."""
    from drivers import workspace
    monkeypatch.setattr(workspace, "CONTROL_ROOT",
                        str(under(root, "/data/engagement-ctl")))
    defn = _defn(root / "defs" / eid, TEMPLATE)
    path = await workspace.provision_workspace(
        engagements_root=str(under(root, "/data/engagements")),
        engagement_id=eid, engagement_auth_token="tok-" + eid[:6],
        defn=defn, task=task, context="ctx", executor_memory=memory,
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework")
    return Path(path)


def legacy_workspace(root: Path, eid: str, block: str, *, cache: str | None,
                     status: str = "COMPLETED",
                     retention_until: str = "2999-01-01T00:00:00Z") -> Path:
    """A workspace as the pre-release ``provision_workspace`` left it: the
    block interpolated raw, its cache beside it (``None``: no cache)."""
    ws = under(root, f"/data/engagements/{eid}")
    write(ws / "CLAUDE.md", TEMPLATE.replace("{task}", "Old task")
          .replace("{context}", "(none)").replace("{executor_memory}", block))
    ctl = under(root, f"/data/engagement-ctl/{eid}")
    ctl.mkdir(parents=True, exist_ok=True)
    if cache is not None:
        write(ctl / ".executor_memory", cache)
    write(ctl / ".casa-meta.json", json.dumps({
        "engagement_id": eid, "executor_type": EXECUTOR_TYPE, "status": status,
        "retention_until": retention_until if status != "UNDERGOING" else None,
    }))
    install_template(root)
    return ws


#: The executor type the stand-in workspaces were provisioned for, and where
#: its template sits on the stand-in install.
EXECUTOR_TYPE = "garden-notes"


def install_template(root: Path, text: str = TEMPLATE) -> Path:
    return write(under(root, f"/config/agents/executors/{EXECUTOR_TYPE}/prompt.md"),
                 text)


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

#: The pre-release writer's order (``drivers/workspace.py:740-772`` before
#: this release, legacy branch): ``CLAUDE.md`` with the block interpolated
#: raw, then the control directory, then the cache, each a plain
#: ``Path.write_text``. Kept here rather than read from history, so the suite
#: needs no git history to run.
_FROZEN_WRITER = """
import os
from pathlib import Path
from drivers import workspace as _current

CONTROL_ROOT = _current.CONTROL_ROOT


async def provision_workspace(*, engagements_root, engagement_id, defn, task,
                              context, executor_memory="", **_ignored):
    _current.CONTROL_ROOT = CONTROL_ROOT
    ws = Path(engagements_root) / engagement_id
    ws.mkdir(parents=True, exist_ok=False)
    prompt_text = Path(defn.prompt_template_path).read_text(encoding="utf-8")
    prompt_interpolated = (
        prompt_text
        .replace("{task}", task or "")
        .replace("{context}", context or "(none)")
        .replace("{executor_type}", defn.type)
        .replace("{executor_memory}", executor_memory or "")
    )
    (ws / "CLAUDE.md").write_text(prompt_interpolated, encoding="utf-8")
    _current.provision_control_dir(engagement_id)
    Path(_current.executor_memory_path(engagement_id)).write_text(
        executor_memory or "", encoding="utf-8")
    return str(ws)
"""


def frozen_workspace_module(dest: Path) -> Path:
    """The pre-release writer, as a loadable file."""
    return write(dest / "frozen_workspace.py", _FROZEN_WRITER)


def provision_in_child(root: Path, eid: str, *, memory: str, hook: str = "",
                       frozen: Path | None = None
                       ) -> subprocess.CompletedProcess:
    """``provision_workspace`` (this release's, or the frozen one) in its own
    process on the stand-in root. *hook* runs first and may arrange a kill."""
    load = ("from drivers import workspace as ws" if frozen is None else
            "import importlib.util as u\n"
            f"spec = u.spec_from_file_location('frozen_workspace', {str(frozen)!r})\n"
            "ws = u.module_from_spec(spec); spec.loader.exec_module(ws)")
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


#: Kill hooks for :func:`provision_in_child`.
KILL_BEFORE_CACHE = """
_real_wt = pathlib.Path.write_text
def _wt(self, *a, **k):
    if self.name == '.executor_memory':
        _die()
    return _real_wt(self, *a, **k)
pathlib.Path.write_text = _wt
"""

KILL_INSIDE_CACHE_WRITE = """
_real_wt = pathlib.Path.write_text
def _wt(self, *a, **k):
    if self.name == '.executor_memory':
        open(self, 'w').close()      # the file is opened and truncated ...
        _die()                       # ... and the process dies before the text
    return _real_wt(self, *a, **k)
pathlib.Path.write_text = _wt
"""


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
