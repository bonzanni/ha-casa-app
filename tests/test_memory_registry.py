"""INV-MEM-025: the memory registry is complete.

Export, restore and the wipe are loops over ``memory.registry.REGISTRY``, so a
store the list does not name is one they silently miss, and nothing else
would notice. Two pins keep the list complete:

* **The store pin** reads a fresh store: every ``sqlite_master`` table and
  every path under ``/data/memory`` must belong to a row, and every row must
  carry all three rules.
* **The producer pin** runs each named producer of memory-derived text with a
  canary, in a sandbox standing in for the root, configured the way the code
  configures it. Every file that holds the canary afterwards, and every
  staging file that holds it when it is renamed into place, is judged by its
  observed full path: the first against the producer's row, the second
  against the row's staging patterns. A read of the producer's code finds its
  persisting call sites, and each must be reached by a run. Every transcript
  root a launcher configures must land on a row.

Each pin's checker is a function, and the red cases feed it a violation it
must report, so a checker that reports nothing is caught too.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import re
import secrets
import sqlite3
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from memory import registry
from memory.registry import REGISTRY, Entry, Export, Kind, Restore, Wipe
from memory.store import MemoryStore

REPO = Path(__file__).resolve().parents[1]
ROOTFS = REPO / "casa" / "rootfs"
CODE_ROOT = ROOTFS / "opt" / "casa"

#: A sample engagement id: placeholders in launchers and ``*`` in row
#: patterns both stand for one of these.
EID = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# Matching a production path against the rows
# ---------------------------------------------------------------------------

def _row_matches(entry: Entry, path: str) -> bool:
    for pattern in entry.paths:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if path.startswith(pattern.rstrip("/") + "/") and entry.kind in (
            Kind.DIRECTORY, Kind.TRANSCRIPTS, Kind.SERVED_COPY,
        ):
            return True
    return False


def _rows_for_path(path: str, entries=REGISTRY) -> list[Entry]:
    return [e for e in entries if e.kind is not Kind.TABLE
            and _row_matches(e, path)]


# ---------------------------------------------------------------------------
# The store pin
# ---------------------------------------------------------------------------

def store_pin_violations(root: str, entries=REGISTRY) -> list[str]:
    """What a store under *root* holds that *entries* do not register, plus
    any entry that lacks a rule."""
    problems: list[str] = []
    names = [e.name for e in entries]
    for name in {n for n in names if names.count(n) > 1}:
        problems.append(f"entry name {name!r} is declared twice")
    for e in entries:
        if not isinstance(e.export, Export):
            problems.append(f"{e.name}: no export rule")
        if not isinstance(e.restore, Restore):
            problems.append(f"{e.name}: no restore rule")
        if not isinstance(e.wipe, Wipe):
            problems.append(f"{e.name}: no wipe rule")
        if not e.paths:
            problems.append(f"{e.name}: declares no path")

    tables = {t for e in entries if e.kind is Kind.TABLE for t in e.paths}
    db = registry.under(root, registry.MEMORY_DB)
    conn = sqlite3.connect(db)
    try:
        present = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    finally:
        conn.close()
    problems += [f"table {t!r} has no registry row"
                 for t in present if t not in tables]

    base = registry.under(root, registry.MEMORY_DIR)
    for dirpath, dirnames, filenames in os.walk(base):
        for name in sorted(dirnames + filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            path = f"{registry.MEMORY_DIR}/{rel}"
            if not _rows_for_path(path, entries):
                problems.append(f"path {path} has no registry row")
    return problems


def _fresh_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(registry.under(str(tmp_path), registry.MEMORY_DIR))
    assert store.open() is True, store.unavailable_reason
    return store


def test_store_pin_every_table_and_path_is_registered(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    # Once with a transaction open, so the WAL sidecars are on disk too.
    with store.transaction():
        held = os.listdir(store.directory)
        assert any(n.endswith("-wal") for n in held), held
        assert store_pin_violations(str(tmp_path)) == []
    assert store_pin_violations(str(tmp_path)) == []


def test_store_pin_red_cases(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    with store.transaction(write=True) as conn:
        conn.execute("CREATE TABLE unregistered_canary (x TEXT)")
    stray = Path(store.directory) / f"stray-{secrets.token_hex(4)}.jsonl"
    stray.write_text("{}\n")
    problems = store_pin_violations(str(tmp_path))
    assert "table 'unregistered_canary' has no registry row" in problems
    assert f"path {registry.MEMORY_DIR}/{stray.name} has no registry row" \
        in problems

    stray.unlink()
    no_rule = tuple(replace(e, wipe=None) if e.name == "meta" else e
                    for e in REGISTRY)
    assert store_pin_violations(str(tmp_path), no_rule) == [
        "meta: no wipe rule", "table 'unregistered_canary' has no registry row",
    ]


# ---------------------------------------------------------------------------
# The producer pin: a small reader of the producers' own code
# ---------------------------------------------------------------------------

#: Calls that persist their argument. The value names the helper family,
#: which decides whether the write leaves a staging file behind on a crash.
_PERSIST_NAMES = {
    "atomic_write_text": "atomic_io",
    "atomic_write_json": "atomic_io",
    "atomic_write_beneath": "safe_fs",
}
_PERSIST_METHODS = {"write_text", "write_bytes", "write", "writelines"}


#: For each named producer, the input that holds the memory-derived text it
#: persists. A new producer joins the registry and this table together.
CARRIES: dict[str, tuple[str, ...]] = {
    "session_registry.py:SessionRegistry._write": ("data",),
    "explanation_store.py:ExplanationStore.record": ("record",),
    "drivers/workspace.py:provision_workspace": ("executor_memory",),
    "drivers/workspace.py:refresh_claude_md": ("executor_memory",),
}


@dataclass(frozen=True)
class Site:
    producer: str      # the registry's producer ref this site was reached from
    module: str        # code-root-relative path
    function: str      # qualname of the function holding the call
    target: str        # the persisted path, as written in the source
    helper: str        # atomic_io | safe_fs | hand_rolled | direct
    line: int
    end_line: int


def _parse(code_root: Path, module: str) -> ast.Module:
    return ast.parse((code_root / module).read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    """Module functions and class methods by qualname."""
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{item.name}"] = item
    return out


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _tainted(fn: ast.AST, seeds: set[str]) -> set[str]:
    """Names in *fn* that may hold text derived from *seeds* (flow-blind)."""
    tainted = set(seeds)
    while True:
        before = len(tainted)
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                value = node.value
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                if value is not None and _names(value) & tainted:
                    for t in targets:
                        tainted |= {n.id for n in ast.walk(t)
                                    if isinstance(n, ast.Name)}
        if len(tainted) == before:
            return tainted


def _helper_of(call: ast.Call, fn: ast.AST) -> str | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)
    if name in _PERSIST_NAMES:
        return _PERSIST_NAMES[name]
    if isinstance(func, ast.Attribute) and name in _PERSIST_METHODS:
        renames = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("replace", "rename")
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"
            for n in ast.walk(fn))
        return "hand_rolled" if renames else "direct"
    return None


def _target_of(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _PERSIST_METHODS:
        return ast.unparse(func.value)
    if isinstance(func, ast.Name) and func.id == "atomic_write_beneath":
        return ", ".join(ast.unparse(a) for a in call.args[:2])
    return ast.unparse(call.args[0]) if call.args else "?"


def producer_sites(code_root: Path, ref: str) -> list[Site]:
    """Every call that persists memory-derived text, reached from producer
    *ref* (``module:qualname``), following the text into functions of the
    same module it is passed to."""
    module, qualname = ref.split(":")
    tree = _parse(code_root, module)
    funcs = _functions(tree)
    sites: list[Site] = []
    todo = [(qualname, set(CARRIES[ref]))]
    seen: set[tuple[str, frozenset[str]]] = set()
    while todo:
        name, seeds = todo.pop()
        key = (name, frozenset(seeds))
        if key in seen:
            continue
        seen.add(key)
        fn = funcs[name]
        tainted = _tainted(fn, seeds)
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            carried = any(_names(a) & tainted for a in
                          [*call.args, *(k.value for k in call.keywords)])
            if not carried:
                continue
            helper = _helper_of(call, fn)
            if helper is not None:
                sites.append(Site(ref, module, name, _target_of(call),
                                  helper, call.lineno,
                                  call.end_lineno or call.lineno))
                continue
            callee = _callee(call, name, funcs)
            if callee is None:
                continue
            params = _params(funcs[callee])
            passed = {params[i] for i, a in enumerate(call.args)
                      if i < len(params) and _names(a) & tainted}
            passed |= {k.arg for k in call.keywords
                       if k.arg and _names(k.value) & tainted}
            todo.append((callee, passed))
    return sorted(sites, key=lambda s: (s.function, s.line))


def _params(fn) -> list[str]:
    args = fn.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    return [n for n in names if n != "self"]


def _callee(call: ast.Call, caller: str, funcs) -> str | None:
    func = call.func
    if isinstance(func, ast.Name) and func.id in funcs:
        return func.id
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "self" and "." in caller):
        qual = f"{caller.split('.')[0]}.{func.attr}"
        return qual if qual in funcs else None
    return None


def _all_producer_sites(code_root: Path, entries=REGISTRY) -> list[Site]:
    return [s for e in entries for ref in e.producer
            for s in producer_sites(code_root, ref)]


# --- production roots, read from the runtime's own code ---------------------

def _module_constant(code_root: Path, module: str, name: str) -> str:
    for node in _parse(code_root, module).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name
                and isinstance(node.value, ast.Constant)):
            return node.value.value
    raise AssertionError(f"{module} has no constant {name}")


def _workspace_dir(engagement_id: str = EID) -> str:
    """The engagement workspace, from the launch template that ``cd``s there."""
    template = (CODE_ROOT / "scripts" / "engagement_run_template.sh").read_text()
    found = re.findall(r'^cd "([^"]+)"$', template, re.M)
    assert len(found) == 1, found
    return found[0].replace("{ID}", engagement_id)


# --- constructor call sites, evaluated statically -----------------------------

class _Unresolved(Exception):
    pass


def _assignments(scope: ast.AST, name: str, before: int) -> list[ast.expr]:
    found = []
    for node in ast.walk(scope):
        if (isinstance(node, ast.Assign) and node.lineno < before
                and any(isinstance(t, ast.Name) and t.id == name
                        for t in node.targets)):
            found.append((node.lineno, node.value))
    return [v for _, v in sorted(found, key=lambda p: p[0])]


def _eval_path(expr: ast.expr, scopes: list[ast.AST], line: int) -> str:
    """A path expression's value, from constants, ``os.path.join``,
    ``Path(...)``, ``/``, ``str(...)`` and names assigned in an enclosing
    scope. Anything else is unresolved, which the pin reports."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        for scope in scopes:
            values = _assignments(scope, expr.id, line if scope is not
                                  scopes[-1] else 10**9)
            if values:
                return _eval_path(values[-1], scopes, line)
        raise _Unresolved(expr.id)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        return os.path.join(_eval_path(expr.left, scopes, line),
                            _eval_path(expr.right, scopes, line))
    if isinstance(expr, ast.Call):
        callee = ast.unparse(expr.func)
        if callee in ("os.path.join", "Path", "str", "pathlib.Path") \
                and expr.args and not expr.keywords:
            return os.path.join(*(_eval_path(a, scopes, line)
                                  for a in expr.args))
    raise _Unresolved(ast.unparse(expr))


def _enclosing_scopes(tree: ast.Module, line: int) -> list[ast.AST]:
    """Innermost function first, the module last."""
    chain = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.lineno <= line <= (n.end_lineno or n.lineno)]
    chain.sort(key=lambda n: n.lineno, reverse=True)
    return [*chain, tree]


def construction_paths(code_root: Path, cls_module: str, cls: str):
    """``(where, path or None)`` for every call of *cls* in the code root,
    with the path argument it is given, or its default when omitted."""
    tree = _parse(code_root, cls_module)
    init = _functions(tree)[f"{cls}.__init__"]
    first = init.args.args[1]                  # the parameter after self
    defaults = dict(zip(
        [a.arg for a in init.args.args][-len(init.args.defaults):],
        init.args.defaults)) if init.args.defaults else {}
    for path in sorted(code_root.rglob("*.py")):
        rel = str(path.relative_to(code_root))
        try:
            mod = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for call in (n for n in ast.walk(mod) if isinstance(n, ast.Call)):
            name = (call.func.id if isinstance(call.func, ast.Name) else
                    call.func.attr if isinstance(call.func, ast.Attribute)
                    else None)
            if name != cls:
                continue
            given = (call.args[0] if call.args else next(
                (k.value for k in call.keywords if k.arg == first.arg), None))
            where = f"{rel}:{call.lineno}"
            try:
                if given is None:
                    yield where, _eval_path(defaults[first.arg], [tree],
                                            init.lineno)
                else:
                    yield where, _eval_path(
                        given, _enclosing_scopes(mod, call.lineno),
                        call.lineno)
            except (_Unresolved, KeyError):
                yield where, None


# --- transcript roots the launchers configure --------------------------------

_SHELL_ASSIGN = re.compile(
    r"""(?:^|[\s;&|(])(?:export\s+)?(HOME|CLAUDE_CONFIG_DIR)="""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s;"']+))""")
_CONTAINER_ENV = re.compile(
    r"""printf\s+'%s'\s+"([^"]+)"\s*\\?\s*>\s*"""
    r"""/run/s6/container_environment/(HOME|CLAUDE_CONFIG_DIR)\b""")
_CONTAINER_ENV_ANY = re.compile(
    r"/run/s6/container_environment/(HOME|CLAUDE_CONFIG_DIR)\b")
_PROJECTS_LITERAL = re.compile(r"""([^\s"'=]*\.claude/projects)\b""")
_SHELL_VAR_DEF = re.compile(r"""^([A-Z_][A-Z0-9_]*)="?([^"\s$]+)"?\s*$""",
                            re.M)


def _is_shell_script(path: Path) -> bool:
    if path.suffix == ".sh":
        return True
    if path.suffix or not path.is_file():
        return False
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return False
    return head.startswith(b"#!") and b"sh" in head.split(b"\n", 1)[0]


def _expand(value: str, defs: dict[str, str]) -> str:
    value = re.sub(r"\$\{?([A-Z_][A-Z0-9_]*)\}?",
                   lambda m: defs.get(m.group(1), m.group(0)), value)
    return value.replace("{ID}", EID)


def shell_transcript_roots(name: str, text: str) -> list[tuple[str, str]]:
    """``(where, root)`` for every transcript root one launcher configures:
    each HOME / CLAUDE_CONFIG_DIR it sets, each one it publishes to the
    container environment, and each literal ``.claude/projects`` path. A
    value that cannot be expanded is returned as is, ``$`` included."""
    code = "\n".join("" if line.lstrip().startswith("#") else line
                     for line in text.splitlines())
    defs = {m.group(1): m.group(2) for m in _SHELL_VAR_DEF.finditer(code)}
    out: list[tuple[str, str]] = []
    for m in _SHELL_ASSIGN.finditer(code):
        value = _expand(next(g for g in m.groups()[1:] if g is not None), defs)
        sub = ".claude/projects" if m.group(1) == "HOME" else "projects"
        out.append((f"{name}: {m.group(0).strip()}",
                    os.path.join(value, sub)))
    published = list(_CONTAINER_ENV.finditer(code))
    if len(published) != len(_CONTAINER_ENV_ANY.findall(code)):
        out.append((f"{name}: a container-environment HOME this scan cannot "
                    "read", "$unparsed"))
    for m in published:
        sub = ".claude/projects" if m.group(2) == "HOME" else "projects"
        out.append((f"{name}: container environment {m.group(2)}",
                    os.path.join(_expand(m.group(1), defs), sub)))
    for m in _PROJECTS_LITERAL.finditer(code):
        out.append((f"{name}: {m.group(1)}", _expand(m.group(1), defs)))
    return out


def python_transcript_roots(code_root: Path) -> list[tuple[str, str]]:
    """A HOME / CLAUDE_CONFIG_DIR that Python code sets for a launch: a dict
    key or a subscript assignment. Unresolvable values come back as ``$``."""
    out = []
    keys = {"HOME": ".claude/projects", "CLAUDE_CONFIG_DIR": "projects"}
    for path in sorted(code_root.rglob("*.py")):
        rel = str(path.relative_to(code_root))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            pairs = []
            if isinstance(node, ast.Dict):
                pairs = [(k, v) for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and k.value in keys]
            elif isinstance(node, ast.Assign):
                pairs = [(t.slice, node.value) for t in node.targets
                         if isinstance(t, ast.Subscript)
                         and isinstance(t.slice, ast.Constant)
                         and t.slice.value in keys]
            for key, value in pairs:
                try:
                    root = _eval_path(value, _enclosing_scopes(
                        tree, node.lineno), node.lineno)
                except _Unresolved:
                    root = "$unresolved"
                out.append((f"{rel}:{node.lineno}",
                            os.path.join(root, keys[key.value])))
    return out


def launcher_transcript_roots() -> list[tuple[str, str]]:
    found = []
    for path in sorted(ROOTFS.rglob("*")):
        if _is_shell_script(path):
            found += shell_transcript_roots(
                str(path.relative_to(ROOTFS)), path.read_text(errors="replace"))
    return found + python_transcript_roots(CODE_ROOT)


# --- running the producers and watching where their text lands ---------------
#
# Each producer is run in a sandbox that stands in for the filesystem root,
# with a random canary as its memory text and the paths its own configuration
# gives it (the construction sites above, the launch template, the module's
# own constants). The pins then judge what was observed: every file that holds
# the canary afterwards, and every file holding it at the instant it is renamed
# into place (the staging file a crash at that instant leaves behind), each by
# its full path. Nothing here names where a producer is expected to write.

#: The modules that hold the named producers.
PRODUCER_MODULES = ("session_registry.py", "explanation_store.py",
                    "drivers/workspace.py")


def _load_modules(code_root: Path) -> dict:
    """The producer modules, imported from *code_root*: the real ones for the
    code root, private copies for a red case's altered tree."""
    import importlib
    import importlib.util
    mods = {}
    for i, rel in enumerate(PRODUCER_MODULES):
        if code_root == CODE_ROOT:
            mods[rel] = importlib.import_module(rel[:-3].replace("/", "."))
        else:
            spec = importlib.util.spec_from_file_location(
                f"_altered_{i}_{Path(rel).stem}", code_root / rel)
            module = importlib.util.module_from_spec(spec)
            # dataclasses resolve string annotations through sys.modules.
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                del sys.modules[spec.name]
            mods[rel] = module
    return mods


@dataclass
class Observed:
    persisted: set[str]        # production paths of files holding the canary
    staged: set[str]           # ... of staging files holding it when renamed
    lines: set[tuple[str, int]]
    error: str | None = None


class _Watch:
    """``os.replace`` stand-in and line tracer for one producer run."""

    def __init__(self, canary: bytes, files: set[str]) -> None:
        self.canary = canary
        self.files = files
        self.real = os.replace
        self.staged: set[str] = set()
        self.lines: set[tuple[str, int]] = set()

    def __call__(self, src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        src_path = os.fspath(src)
        if src_dir_fd is not None and not os.path.isabs(src_path):
            src_path = os.path.join(
                os.readlink(f"/proc/self/fd/{src_dir_fd}"), src_path)
        try:
            if self.canary in Path(src_path).read_bytes():
                self.staged.add(os.path.abspath(src_path))
        except OSError:
            pass
        return self.real(src, dst, src_dir_fd=src_dir_fd,
                         dst_dir_fd=dst_dir_fd)

    def trace(self, frame, event, arg):
        if frame.f_code.co_filename not in self.files:
            return None

        def local(frame, event, arg):
            if event == "line":
                self.lines.add((frame.f_code.co_filename, frame.f_lineno))
            return local
        return local


def _production(path: str, sandbox: Path) -> str:
    """The production path a sandbox path stands for; a path outside the
    sandbox is returned as is, and no row covers ``/tmp``."""
    rel = os.path.relpath(path, sandbox)
    return path if rel.startswith("..") else "/" + rel


async def _observe(exercise, sandbox: Path, mods: dict) -> Observed:
    canary = secrets.token_hex(16)
    files = {os.path.abspath(m.__file__) for m in mods.values()}
    watch = _Watch(canary.encode(), files)
    previous = sys.gettrace()
    error = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", watch)
        sys.settrace(watch.trace)
        try:
            await exercise(sandbox, canary, mods, mp)
        except Exception as exc:  # noqa: BLE001 — reported as a violation
            error = f"{type(exc).__name__}: {exc}"
        finally:
            sys.settrace(previous)
    persisted = set()
    for dirpath, _, filenames in os.walk(sandbox):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not stat.S_ISREG(os.lstat(full).st_mode):
                continue        # a workspace holds a FIFO; never open one
            try:
                if canary.encode() in Path(full).read_bytes():
                    persisted.add(_production(full, sandbox))
            except OSError:
                pass
    return Observed(persisted, {_production(p, sandbox) for p in watch.staged},
                    watch.lines, error)


def _sandboxed(sandbox: Path, production: str) -> Path:
    path = Path(registry.under(str(sandbox), production))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _constructed(code_root: Path, module: str, cls: str) -> list[str]:
    paths = []
    for where, path in construction_paths(code_root, module, cls):
        if path is None:
            raise AssertionError(f"{where}: {cls} is given a path this "
                                 "scan cannot resolve")
        paths.append(path)
    if not paths:
        raise AssertionError(f"nothing constructs {cls}")
    return paths


async def _run_session_registry(sandbox, canary, mods, mp, code_root):
    module = mods["session_registry.py"]
    for path in _constructed(code_root, "session_registry.py",
                             "SessionRegistry"):
        reg = module.SessionRegistry(str(_sandboxed(sandbox, path)))
        reg._write({"telegram-sample": {"sdk_session_id": canary}})


async def _run_explanation_store(sandbox, canary, mods, mp, code_root):
    module = mods["explanation_store.py"]
    for path in _constructed(code_root, "explanation_store.py",
                             "ExplanationStore"):
        module.ExplanationStore(_sandboxed(sandbox, path)).record(
            module.ExplanationRecord(
                correlation_id="a1b2c3d4", role_id="resident:assistant",
                kind="resident", resolved_model="model", persona_ref=None,
                role_checksum="0" * 8, binding_digest=None,
                dependency_digests=(), effective_config_digest=None,
                lifecycle_state=None, projection="telegram",
                static_prompt_digest="0" * 8,
                static_prompt_estimated_tokens=1, memory_tiers=(),
                memory_attributions=(), tool_calls=(), denials=(),
                memory_text=canary))


def _executor(tmp: Path, name: str, *, template: bool):
    from config import ExecutorDefinition
    try:
        from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
    except ImportError:
        from role_artifact_stub import STUB_ROLE_ARTIFACT
    exec_dir = tmp / name
    exec_dir.mkdir(parents=True)
    (exec_dir / "prompt.md").write_text("TASK:{task}\nMEM:{executor_memory}\n")
    if template:
        (exec_dir / "workspace-template").mkdir()
        (exec_dir / "workspace-template" / "CLAUDE.md.tmpl").write_text(
            "TASK:{task}\nMEM:{executor_memory}\n")
    return ExecutorDefinition(
        role_artifact=STUB_ROLE_ARTIFACT, type=name,
        description="a sample executor for the producer pin",
        model="sonnet", driver="claude_code",
        prompt_template_path=str(exec_dir / "prompt.md"),
        mcp_server_names=["casa-framework"],
    )


def _record(engagement_id: str, executor_type: str):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=engagement_id, kind="executor", role_or_type=executor_type,
        driver="claude_code", status="active", topic_id=1, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, origin={}, task="sample task",
    )


async def _provision(sandbox, canary, mods, mp, *, template: bool) -> Path:
    """One workspace, legacy or template branch, at the workspace path the
    launch template uses and with the module's own control root."""
    module = mods["drivers/workspace.py"]
    control = _module_constant(CODE_ROOT, "drivers/workspace.py",
                               "CONTROL_ROOT")
    mp.setattr(module, "CONTROL_ROOT", str(_sandboxed(sandbox, control + "/x")
                                           .parent))
    eid = secrets.token_hex(16)
    ws = Path(registry.under(str(sandbox), _workspace_dir(eid)))
    ws.parent.mkdir(parents=True, exist_ok=True)
    defn = _executor(sandbox.parent / f"defs-{eid}", "sample-executor",
                     template=template)
    path = await module.provision_workspace(
        engagements_root=str(ws.parent), engagement_id=eid,
        engagement_auth_token="token-sample", defn=defn, task="sample task",
        context="", casa_framework_mcp_url="http://127.0.0.1:1/mcp",
        workspace_template_root=(Path(defn.prompt_template_path).parent
                                 / "workspace-template") if template else None,
        executor_memory=canary,
    )
    return Path(path), defn


async def _run_provision(sandbox, canary, mods, mp, code_root):
    for template in (False, True):
        await _provision(sandbox, canary, mods, mp, template=template)


async def _run_refresh(sandbox, canary, mods, mp, code_root):
    module = mods["drivers/workspace.py"]
    for template in (False, True):
        ws, defn = await _provision(sandbox, canary, mods, mp,
                                    template=template)
        (ws / "CLAUDE.md").write_text("")
        module.refresh_claude_md(str(ws), defn=defn,
                                 rec=_record(ws.name, defn.type))


#: One way to run each named producer.
EXERCISERS = {
    "session_registry.py:SessionRegistry._write": _run_session_registry,
    "explanation_store.py:ExplanationStore.record": _run_explanation_store,
    "drivers/workspace.py:provision_workspace": _run_provision,
    "drivers/workspace.py:refresh_claude_md": _run_refresh,
}


async def observe_producers(code_root: Path, tmp: Path, entries=REGISTRY):
    """``[(entry, ref, Observed)]`` for every producer every row names."""
    mods = _load_modules(code_root)
    out = []
    for i, (entry, ref) in enumerate(
            (e, r) for e in entries for r in e.producer):
        sandbox = tmp / f"run-{i}" / "root"
        sandbox.mkdir(parents=True)
        run = EXERCISERS.get(ref)
        if run is None:
            out.append((entry, ref, Observed(set(), set(), set(),
                                             "no exerciser for this producer")))
            continue

        async def exercise(sb, canary, m, mp, _run=run):
            await _run(sb, canary, m, mp, code_root)
        out.append((entry, ref, await _observe(exercise, sandbox, mods)))
    return out


# --- the pins --------------------------------------------------------------

def producer_pin_violations(code_root: Path, observations, roots,
                            entries=REGISTRY) -> list[str]:
    problems: list[str] = []
    named = {ref for e in entries for ref in e.producer}
    for ref in sorted(set(EXERCISERS) - named):
        problems.append(f"stale exerciser, no row names {ref}")
    for entry, ref, seen in observations:
        if seen.error:
            problems.append(f"{ref}: {seen.error}")
            continue
        if not seen.persisted:
            problems.append(f"{ref}: its run persisted no memory text")
        for path in sorted(seen.persisted):
            if not _row_matches(entry, path):
                problems.append(f"{ref} persisted memory text to {path}, "
                                f"outside row {entry.name!r}")
        module = os.path.abspath(code_root / ref.split(":")[0])
        for site in producer_sites(code_root, ref):
            span = range(site.line, site.end_line + 1)
            if not any((module, n) in seen.lines for n in span):
                problems.append(
                    f"{site.module}:{site.line} ({site.function}) persists "
                    f"memory text to {site.target!r}, and no run of {ref} "
                    "reached it")

    transcript_rows = [e for e in entries if e.kind is Kind.TRANSCRIPTS]
    for where, root in roots:
        if "$" in root:
            problems.append(f"{where}: a transcript root this scan cannot "
                            "resolve")
        elif not any(_row_matches(e, root) for e in transcript_rows):
            problems.append(f"{where}: transcript root {root} has no "
                            "registry row")
    return problems


def staging_violations(observations) -> list[str]:
    problems = []
    for entry, ref, seen in observations:
        for path in sorted(seen.staged):
            if not any(fnmatch.fnmatchcase(path, p)
                       for p in entry.staging_patterns):
                problems.append(f"{ref} staged memory text at {path}, which "
                                f"row {entry.name!r} does not declare")
    return problems


async def test_producer_pin_every_persisting_call_site_has_a_row(
    tmp_path: Path,
) -> None:
    roots = launcher_transcript_roots()
    # The scan must see the launchers it exists for, or it pins nothing.
    seen = {root for _, root in roots}
    assert "/config/cc-home/.claude/projects" in seen, roots
    assert f"/data/engagements/{EID}/.home/.claude/projects" in seen, roots
    assert "/root/.claude/projects" in seen, roots
    observations = await observe_producers(CODE_ROOT, tmp_path)
    assert {ref for _, ref, _ in observations} == set(EXERCISERS)
    assert producer_pin_violations(CODE_ROOT, observations, roots) == []


async def test_every_producer_declares_its_staging_pattern(
    tmp_path: Path,
) -> None:
    observations = await observe_producers(CODE_ROOT, tmp_path)
    staged = {ref for _, ref, seen in observations if seen.staged}
    # Every producer that writes through an atomic helper was seen staging.
    assert staged == {
        "session_registry.py:SessionRegistry._write",
        "explanation_store.py:ExplanationStore.record",
        "drivers/workspace.py:refresh_claude_md",
    }, observations
    assert staging_violations(observations) == []


# --- red cases ---------------------------------------------------------------

def _altered_tree(tmp_path: Path, module: str, old: str, new: str) -> Path:
    """A copy of the code the pins read, with one producer changed."""
    root = tmp_path / "altered"
    for rel in (*PRODUCER_MODULES, "casa_core.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text((CODE_ROOT / rel).read_text())
    text = (root / module).read_text()
    assert text.count(old) == 1, old
    (root / module).write_text(text.replace(old, new))
    return root


async def _violations(tmp_path: Path, module: str, old: str, new: str,
                      check=producer_pin_violations) -> list[str]:
    root = _altered_tree(tmp_path, module, old, new)
    observations = await observe_producers(root, tmp_path / "runs")
    if check is producer_pin_violations:
        return check(root, observations, [])
    return check(observations)


_PROVISION_ANCHOR = "    provision_control_dir(engagement_id)\n"


async def test_producer_pin_red_cases(tmp_path: Path) -> None:
    # 1. A producer persisting its text to a path no row covers.
    problems = await _violations(
        tmp_path / "1", "drivers/workspace.py", _PROVISION_ANCHOR,
        _PROVISION_ANCHOR
        + "    (ws / 'MEMORY.md').write_text(executor_memory)\n")
    assert any(re.search(r"provision_workspace persisted memory text to "
                         rf"/data/engagements/[0-9a-f]{{32}}/MEMORY\.md, "
                         "outside row 'executor memory'", p)
               for p in problems), problems

    # 2. The same producer and constructor, but the file it writes moved:
    #    only the observed destination shows it.
    problems = await _violations(
        tmp_path / "2", "explanation_store.py",
        'return self._root / f"{correlation_id}.json"',
        'return self._root.parent / f"{correlation_id}.json"')
    assert ("explanation_store.py:ExplanationStore.record persisted memory "
            "text to /data/a1b2c3d4.json, outside row 'explanations'"
            ) in problems, problems

    # 3. A persisting site no run reaches.
    problems = await _violations(
        tmp_path / "3", "drivers/workspace.py", _PROVISION_ANCHOR,
        _PROVISION_ANCHOR + "    if task == 'never-run':\n"
        "        (ws / 'NEVER.md').write_text(executor_memory)\n")
    assert any("persists memory text to \"ws / 'NEVER.md'\", and no run of "
               "drivers/workspace.py:provision_workspace reached it" in p
               for p in problems), problems

    # 4. A launcher configuring a transcript root with no row.
    roots = shell_transcript_roots(
        "etc/s6-overlay/scripts/new-service.sh",
        "#!/bin/bash\nexport HOME=/data/other-home\nexec claude\n")
    assert roots == [("etc/s6-overlay/scripts/new-service.sh: "
                      "export HOME=/data/other-home",
                      "/data/other-home/.claude/projects")]
    assert producer_pin_violations(CODE_ROOT, [], roots) == [
        "etc/s6-overlay/scripts/new-service.sh: export HOME=/data/other-home: "
        "transcript root /data/other-home/.claude/projects has no registry row"]


async def test_staging_pin_red_case(tmp_path: Path) -> None:
    # 1. The shared writer's generic name: what sessions.json staged as before
    #    its writer named the target.
    problems = await _violations(
        tmp_path / "1", "session_registry.py",
        "mode=PRIVATE, prefix=_STAGING_PREFIX)", "mode=PRIVATE)",
        check=staging_violations)
    assert len(problems) == 1 and re.fullmatch(
        r"session_registry\.py:SessionRegistry\._write staged memory text at "
        r"/data/\.[^/]+\.tmp, which row 'session pointers' does not declare",
        problems[0]), problems

    # 2. A staging file with its declared name, in another directory.
    problems = await _violations(
        tmp_path / "2", "explanation_store.py",
        'temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")',
        'temporary = self._root.parent / f"{path.stem}.{uuid.uuid4().hex}.tmp"',
        check=staging_violations)
    assert len(problems) == 1 and re.fullmatch(
        r"explanation_store\.py:ExplanationStore\.record staged memory text "
        r"at /data/a1b2c3d4\.[0-9a-f]{32}\.tmp, which row 'explanations' "
        r"does not declare", problems[0]), problems
