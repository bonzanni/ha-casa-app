"""INV-MEM-025: the memory registry is complete.

Export, restore and the wipe are loops over ``memory.registry.REGISTRY``, so a
store the list does not name is one they silently miss, and nothing else
would notice. Two pins keep the list complete:

* **The store pin** reads a fresh store, and the same store once the capture
  producers have written to it (a committed item, a spool, and the snapshot
  a cut-off drain leaves): every
  ``sqlite_master`` table and every path under ``/data/memory`` must belong to
  a row, and every row must carry all three rules.
* **The producer pin** runs each named producer of memory-derived text in a
  sandbox standing in for the root, configured the way the code configures it
  and with its optional memory-bearing inputs filled. An audit hook records every
  write it issues, as it is issued, with its full path and the producer line
  that issued it. A write from a memory-bearing call site (found by reading
  the producer's code) must land in the producer's row, a staged one on the
  row's staging patterns; any other write must land in the row or on the
  short list of files the producer writes that carry no memory. Every
  memory-bearing call site must have issued a write. Every transcript root a
  launcher configures must land on a row.

  It catches memory written to a new or moved location. Memory placed inside a
  file on the no-memory list, and a write that happens only under inputs the
  runs do not produce, are left to code review.

Each pin's checker is a function, and the red cases feed it a violation it
must report, so a checker that reports nothing is caught too.
"""
from __future__ import annotations

import ast
import os
import re
import secrets
import sqlite3
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

# ---------------------------------------------------------------------------
# Matching a production path against the rows
# ---------------------------------------------------------------------------

def _glob(path: str, pattern: str) -> bool:
    """``*`` matches within one path segment, never across a ``/``."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", "[^/]*"),
                        path) is not None


def _row_matches(entry: Entry, path: str) -> bool:
    for pattern in entry.paths:
        if _glob(path, pattern):
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


def _run_capture(store: MemoryStore) -> None:
    """The producers that write under ``/data/memory``: a capture that
    commits, one that spools while the store is down, a drain cut off by the
    store after it took the spool (leaving its snapshot), and one more
    spooled capture (a fresh spool beside the snapshot)."""
    from memory import capture
    from memory.store import MemoryUnavailable
    item = capture.event(role="assistant", channel="telegram",
                         speaker_kind="resident", origin_kind="operator",
                         text="a sample remembered fact",
                         eligibility="resident_message", origin_tier="private")
    assert capture.append(store, item).outcome == capture.STORED
    store._unavailable_reason = "a simulated outage"
    try:
        spooled = capture.append(store, replace(item, source_key="event:1"))
        with pytest.raises(MemoryUnavailable):
            capture.drain_spool(store)
        again = capture.append(store, replace(item, source_key="event:2"))
    finally:
        store._unavailable_reason = None
    assert spooled.outcome == again.outcome == capture.SPOOLED


def test_store_pin_every_table_and_path_is_registered(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    # Once with a transaction open, so the WAL sidecars are on disk too.
    with store.transaction():
        held = os.listdir(store.directory)
        assert any(n.endswith("-wal") for n in held), held
        assert store_pin_violations(str(tmp_path)) == []
    assert store_pin_violations(str(tmp_path)) == []
    # And after the capture producers have written: the spool is a path too.
    _run_capture(store)
    assert {"spool.jsonl", "spool.draining.jsonl"} <= set(
        os.listdir(store.directory))
    assert store_pin_violations(str(tmp_path)) == []
    # And the derived search index, once built.
    from memory import search
    assert search.ensure(store) is True
    assert "search.db" in os.listdir(store.directory)
    assert store_pin_violations(str(tmp_path)) == []
    # And the export and the restore: a published export, one cut off
    # before its publication, a staged restore source and the store a
    # restore builds there.
    _run_export_and_restore(store, tmp_path)
    assert store_pin_violations(str(tmp_path)) == []


def _run_export_and_restore(store: MemoryStore, root: Path) -> None:
    from memory import export, maintenance, restore
    published = export.export(store)
    parent = export.exports_dir(store)
    os.makedirs(os.path.join(parent, ".export-cut-off.part"))
    assert restore.stage(published, root=str(root), confirm=True) == "staged"
    intent = maintenance.read_intent(str(root))
    assert restore._build(restore.source_dir(str(root)), intent, None)
    held = set(os.listdir(restore.source_dir(str(root))))
    assert {"built.db", "manifest.json", "events.jsonl"} <= held, held


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
    # A capture producer that writes somewhere no row declares.
    from memory import capture
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(capture, "SPOOL_NAME", "stray-capture.jsonl")
        mp.setattr(capture, "SNAPSHOT_NAME", "stray-snapshot.jsonl")
        _run_capture(store)
    problems = store_pin_violations(str(tmp_path))
    for stray_name in ("stray-capture.jsonl", "stray-snapshot.jsonl"):
        assert (f"path {registry.MEMORY_DIR}/{stray_name} has no registry "
                "row") in problems
        (Path(store.directory) / stray_name).unlink()

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
}
_PERSIST_METHODS = {"write_text", "write_bytes", "write", "writelines"}


#: For each named producer, the input that holds the memory-derived text it
#: persists. A new producer joins the registry and this table together.
CARRIES: dict[str, tuple[str, ...]] = {
    "memory/view.py:serve": ("view",),
    "session_registry.py:SessionRegistry._write": ("data",),
    "explanation_store.py:ExplanationStore.record": ("record",),
    # The index copies each event's text from the store's rows.
    "memory/search.py:catch_up": ("rows",),
}


@dataclass(frozen=True)
class Site:
    producer: str      # the registry's producer ref this site was reached from
    module: str        # code-root-relative path
    function: str      # qualname of the function holding the call
    target: str        # the persisted path, as written in the source
    helper: str        # atomic_io | hand_rolled | direct
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
    return value


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


# --- running the producers and recording every write they make --------------
#
# Each producer is run in a sandbox that stands in for the filesystem root,
# with the configuration the code gives it (the construction sites above) and
# every memory-bearing input filled, so conditional writes happen. An audit hook (``sys.addaudithook``)
# records each write as it is issued — an ``open`` for writing or creating, a
# rename's destination, a directory, a link, a truncation, a SQLite database —
# with its full resolved path and the producer frame (file and line) that
# issued it. Nothing is inferred from what the files hold afterwards, so a
# file written and then deleted, or holding an encoded copy, is still seen.

#: The modules that hold the named producers.
PRODUCER_MODULES = ("session_registry.py", "explanation_store.py",
                    "memory/view.py", "memory/search.py")

#: Writes a producer makes that carry no memory, by row. A write from a
#: memory-bearing call site is never judged against this list. The pin judges
#: where a write lands, not what it holds, so memory placed inside one of these
#: files through some other route is code review's to catch.
NON_MEMORY = {
    # The indexer opens the store it indexes (to read it); the audit event
    # of a SQLite open does not say read or write.
    "search index": (registry.MEMORY_DB,),
}


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


@dataclass(frozen=True)
class Write:
    path: str          # production path, or the raw path outside the sandbox
    event: str
    module: str        # code-root-relative module of the issuing frame
    line: int          # its line
    source: str | None = None   # a rename's source, the staging file


_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
                | os.O_APPEND)


class _Recorder:
    """What one producer run writes, from the audit events it raises."""

    def __init__(self, sandbox: Path, files: dict[str, str]) -> None:
        self.sandbox = sandbox
        self.files = files                  # abs file -> code-root rel path
        self.writes: list[Write] = []
        self.pending: dict[str, tuple[str, str, int]] = {}
        self.errors: list[str] = []

    def _issuer(self):
        frame = sys._getframe(3)            # past the hook and its dispatch
        while frame is not None:
            rel = self.files.get(frame.f_code.co_filename)
            if rel is not None:
                return rel, frame.f_lineno
            frame = frame.f_back
        return None

    def _resolve(self, path, dir_fd) -> str | None:
        if isinstance(path, int) or path is None:
            return None
        path = os.fsdecode(os.fspath(path))
        if os.path.isabs(path):
            return os.path.normpath(path)
        if dir_fd is not None and dir_fd >= 0:
            return os.path.normpath(os.path.join(
                os.readlink(f"/proc/self/fd/{dir_fd}"), path))
        return None

    def _shown(self, path: str) -> str:
        rel = os.path.relpath(path, self.sandbox)
        return path if rel.startswith("..") else "/" + rel

    def _record(self, path: str, event: str, issuer, source=None) -> None:
        self.writes.append(Write(self._shown(path), event, issuer[0],
                                 issuer[1], source))

    def __call__(self, event: str, args) -> None:
        if event not in ("open", "os.rename", "os.mkdir", "os.symlink",
                         "os.link", "os.truncate", "os.remove",
                         "sqlite3.connect"):
            return
        issuer = self._issuer()
        if issuer is None:
            return                          # the test's own set-up
        if event == "open":
            path, mode, flags = args
            if mode is None:                # os.open: flags, maybe a dir_fd
                if not flags & _WRITE_FLAGS:
                    return
                if isinstance(path, int):
                    return
                name = os.fsdecode(os.fspath(path))
                if not os.path.isabs(name):
                    # The event does not carry os.open's dir_fd; the rename
                    # or removal of the same name, which does, places it.
                    self.pending[name] = ("open", *issuer)
                    return
                self._record(os.path.normpath(name), event, issuer)
            elif any(c in mode for c in "wax+") and not isinstance(path, int):
                name = os.fsdecode(os.fspath(path))
                self._record(os.path.abspath(name), event, issuer)
        elif event in ("os.rename", "os.link"):
            src, dst, src_dir_fd, dst_dir_fd = args
            self._settle(src, src_dir_fd)
            target = self._resolve(dst, dst_dir_fd)
            source = self._resolve(src, src_dir_fd)
            if target is not None:
                self._record(target, event, issuer,
                             source and self._shown(source))
        elif event == "os.remove":
            self._settle(*args)
        elif event in ("os.mkdir", "os.symlink"):
            path = args[0] if event == "os.mkdir" else args[1]
            target = self._resolve(path, args[-1])
            if target is not None:
                self._record(target, event, issuer)
        elif event == "os.truncate":
            target = self._resolve(args[0], None)
            if target is not None:
                self._record(target, event, issuer)
        elif event == "sqlite3.connect":
            target = self._resolve(args[0], None)
            if target is not None:
                self._record(target, event, issuer)

    def _settle(self, path, dir_fd) -> None:
        if isinstance(path, int):
            return
        name = os.fsdecode(os.fspath(path))
        if name in self.pending:
            event, module, line = self.pending.pop(name)
            target = self._resolve(name, dir_fd)
            if target is not None:
                self._record(target, event, (module, line))


#: The recorder of the producer run in progress. An audit hook cannot be
#: removed, so one is installed per process and does nothing in between.
_ACTIVE: list[_Recorder | None] = [None]


def _audit(event: str, args) -> None:
    recorder = _ACTIVE[0]
    if recorder is None:
        return
    try:
        recorder(event, args)
    except Exception as exc:  # noqa: BLE001 — never break the audited call
        recorder.errors.append(f"{event}: {type(exc).__name__}: {exc}")


_HOOKED: list[bool] = []


def _hook() -> None:
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED.append(True)


@dataclass
class Observed:
    writes: list[Write]
    error: str | None = None


async def _observe(exercise, sandbox: Path, mods: dict,
                   code_root: Path) -> Observed:
    _hook()
    files = {os.path.abspath(m.__file__): rel for rel, m in mods.items()}
    recorder = _Recorder(sandbox, files)
    error = None
    with pytest.MonkeyPatch.context() as mp:
        _ACTIVE[0] = recorder
        try:
            await exercise(sandbox, mods, mp, code_root)
        except Exception as exc:  # noqa: BLE001 — reported as a violation
            error = f"{type(exc).__name__}: {exc}"
        finally:
            _ACTIVE[0] = None
    problems = list(recorder.errors)
    problems += [f"{module}:{line} wrote {name!r}, whose directory the audit "
                 "events never named" for name, (_, module, line)
                 in recorder.pending.items()]
    if error:
        problems.insert(0, error)
    return Observed(recorder.writes, "; ".join(problems) or None)


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


#: Memory text the runs carry; any non-empty text does.
MEMORY = "a sample remembered fact"


async def _run_session_registry(sandbox, mods, mp, code_root):
    module = mods["session_registry.py"]
    for path in _constructed(code_root, "session_registry.py",
                             "SessionRegistry"):
        reg = module.SessionRegistry(str(_sandboxed(sandbox, path)))
        reg._write({"telegram-sample": {"sdk_session_id": MEMORY,
                                        "agent": "resident:assistant"}})


async def _run_explanation_store(sandbox, mods, mp, code_root):
    module = mods["explanation_store.py"]
    for path in _constructed(code_root, "explanation_store.py",
                             "ExplanationStore"):
        module.ExplanationStore(_sandboxed(sandbox, path)).record(
            module.ExplanationRecord(
                correlation_id="a1b2c3d4", role_id="resident:assistant",
                kind="resident", resolved_model="model", persona_ref="p",
                role_checksum="0" * 8, binding_digest="d",
                dependency_digests=("d",), effective_config_digest="d",
                lifecycle_state="active", projection="telegram",
                static_prompt_digest="0" * 8,
                static_prompt_estimated_tokens=1, memory_tiers=("private",),
                memory_attributions=("a",), tool_calls=("t",),
                denials=("d",), system_prompt=MEMORY, memory_text=MEMORY))


async def _run_view_serve(sandbox, mods, mp, code_root):
    """A session served its view of a ledger holding one entry: a new
    session, its view bound to its SDK session, then that session resumed."""
    from memory import patches
    module = mods["memory/view.py"]
    store = MemoryStore(str(_sandboxed(sandbox, registry.MEMORY_DIR + "/x")
                            .parent))
    assert store.open(), store.unavailable_reason
    with store.transaction(write=True) as conn:
        patches.apply(conn, "private", [
            {"op": "add", "kind": "entry", "entry_kind": "fact",
             "topic": "Home", "subject": "sample", "body": MEMORY,
             "line": "a sample need"}],
            job_id="job", commit_no=1, committed_at="2026-01-01T00:00:00+00:00")
    served = module.serve(store, "telegram-sample", clearance="private",
                          budget=1000)
    assert MEMORY in served.view.text and not served.reset
    assert module.bind(store, "telegram-sample", served.view, "sid-sample")
    served = module.serve(store, "telegram-sample", clearance="private",
                          budget=1000, resume_sid="sid-sample")
    assert MEMORY in served.view.text and not served.reset


async def _run_search_index(sandbox, mods, mp, code_root):
    """The index of a store holding one event: built, then caught up with a
    second, then rebuilt."""
    from memory import capture
    module = mods["memory/search.py"]
    store = MemoryStore(str(_sandboxed(sandbox, registry.MEMORY_DIR + "/x")
                            .parent))
    assert store.open(), store.unavailable_reason
    for text in (MEMORY, "a second sample"):
        item = capture.event(role="assistant", channel="telegram",
                             speaker_kind="operator", origin_kind="operator",
                             text=text, eligibility="write_trusted_turn",
                             origin_tier="private")
        assert capture.append(store, item).outcome == capture.STORED
        module.catch_up(store)
    module.rebuild(store)
    found = module.search(store, "sample remembered", clearance="private")
    assert [h["text"] for h in found.hits][0] == MEMORY


#: One way to run each named producer.
EXERCISERS = {
    "memory/search.py:catch_up": _run_search_index,
    "memory/view.py:serve": _run_view_serve,
    "session_registry.py:SessionRegistry._write": _run_session_registry,
    "explanation_store.py:ExplanationStore.record": _run_explanation_store,
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
            out.append((entry, ref, Observed([], "no way to run it")))
            continue
        out.append((entry, ref, await _observe(run, sandbox, mods, code_root)))
    return out


# --- the pins --------------------------------------------------------------

def _covered(entry: Entry, path: str, extra=()) -> bool:
    return _row_matches(entry, path) or any(
        _glob(path, p)
        for p in (*entry.staging_patterns, *extra))


def _memory_sites(code_root: Path, entries=REGISTRY) -> dict[str, list[Site]]:
    return {ref: producer_sites(code_root, ref)
            for e in entries for ref in e.producer}


def _site_of(write: Write, sites) -> Site | None:
    return next((s for s in sites if s.module == write.module
                 and s.line <= write.line <= s.end_line), None)


def producer_pin_violations(code_root: Path, observations, roots,
                            entries=REGISTRY) -> list[str]:
    problems: list[str] = []
    named = {ref for e in entries for ref in e.producer}
    for ref in sorted(set(EXERCISERS) - named):
        problems.append(f"stale run, no row names {ref}")
    by_ref = _memory_sites(code_root, entries)
    every_site = [s for sites in by_ref.values() for s in sites]
    for entry, ref, seen in observations:
        if seen.error:
            problems.append(f"{ref}: {seen.error}")
        for w in seen.writes:
            site = _site_of(w, every_site)
            allowed = () if site else NON_MEMORY.get(entry.name, ())
            if not _covered(entry, w.path, allowed):
                kind = (f"memory text from {site.function} ({site.target!r})"
                        if site else "a file it was never declared to write")
                problems.append(f"{w.module}:{w.line} writes {w.path} "
                                f"({w.event}): {kind}, outside row "
                                f"{entry.name!r}")
        for site in by_ref.get(ref, []):
            if not any(_site_of(w, [site]) for w in seen.writes):
                problems.append(
                    f"{site.module}:{site.line} ({site.function}) persists "
                    f"memory text to {site.target!r}, and no run of {ref} "
                    "made it write")

    transcript_rows = [e for e in entries if e.kind is Kind.TRANSCRIPTS]
    for where, root in roots:
        if "$" in root:
            problems.append(f"{where}: a transcript root this scan cannot "
                            "resolve")
        elif not any(_row_matches(e, root) for e in transcript_rows):
            problems.append(f"{where}: transcript root {root} has no "
                            "registry row")
    return problems


def staged_files(code_root: Path, observations, entries=REGISTRY):
    """``(entry, ref, path)`` for every file a memory-bearing call site
    creates and later renames into place: the file a crash before the rename
    leaves behind."""
    every_site = [s for sites in _memory_sites(code_root, entries).values()
                  for s in sites]
    for entry, ref, seen in observations:
        sources = {w.source for w in seen.writes if w.source}
        for w in seen.writes:
            if w.event == "open" and w.path in sources \
                    and _site_of(w, every_site):
                yield entry, ref, w.path


def staging_violations(code_root: Path, observations,
                       entries=REGISTRY) -> list[str]:
    """Each staged file must match one of its row's staging patterns."""
    return [f"{ref} staged memory text at {path}, which row {entry.name!r} "
            "does not declare"
            for entry, ref, path in staged_files(code_root, observations,
                                                 entries)
            if not any(_glob(path, p)
                       for p in entry.staging_patterns)]


async def test_producer_pin_every_persisting_call_site_has_a_row(
    tmp_path: Path,
) -> None:
    roots = launcher_transcript_roots()
    # The scan must see the launchers it exists for, or it pins nothing.
    seen = {root for _, root in roots}
    assert "/config/cc-home/.claude/projects" in seen, roots
    assert "/root/.claude/projects" in seen, roots
    observations = await observe_producers(CODE_ROOT, tmp_path)
    assert {ref for _, ref, _ in observations} == set(EXERCISERS)
    assert producer_pin_violations(CODE_ROOT, observations, roots) == []


async def test_every_producer_declares_its_staging_pattern(
    tmp_path: Path,
) -> None:
    observations = await observe_producers(CODE_ROOT, tmp_path)
    staged = {ref for _, ref, _ in staged_files(CODE_ROOT, observations)}
    # Every producer that writes through an atomic helper was seen staging.
    assert staged == {
        "session_registry.py:SessionRegistry._write",
        "explanation_store.py:ExplanationStore.record",
    }, observations
    assert staging_violations(CODE_ROOT, observations) == []


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
                      staging: bool = False) -> list[str]:
    root = _altered_tree(tmp_path, module, old, new)
    observations = await observe_producers(root, tmp_path / "runs")
    if staging:
        return staging_violations(root, observations)
    return producer_pin_violations(root, observations, [])


_RECORD_ANCHOR = "        path = self._path(record.correlation_id)\n"
_OUTSIDE = re.compile(
    r"explanation_store\.py:\d+ writes /data/(?P<name>\S+) \(open\): memory "
    r"text from ExplanationStore\.record .*, outside row 'explanations'")
#: The memory text the record carries, and the directory beside its row.
_TEXT = "record.memory_text"
_BESIDE = "(self._root.parent / {name!r})"


def _outside_names(problems: list[str]) -> set[str]:
    return {m.group("name") for p in problems if (m := _OUTSIDE.fullmatch(p))}


async def test_producer_pin_red_cases(tmp_path: Path) -> None:
    # 1. A producer persisting its text to a path no row covers.
    problems = await _violations(
        tmp_path / "1", "explanation_store.py", _RECORD_ANCHOR,
        _RECORD_ANCHOR
        + f"        {_BESIDE.format(name='MEMORY.md')}.write_text({_TEXT})\n")
    assert _outside_names(problems) == {"MEMORY.md"}, problems

    # 2. The same, behind a condition on another input.
    problems = await _violations(
        tmp_path / "2", "explanation_store.py", _RECORD_ANCHOR,
        _RECORD_ANCHOR + "        if record.tool_calls: "
        f"{_BESIDE.format(name='MEMORY.md')}.write_text({_TEXT})\n")
    assert _outside_names(problems) == {"MEMORY.md"}, problems

    # 3. An encoded copy: the content says nothing, the write does.
    problems = await _violations(
        tmp_path / "3", "explanation_store.py", _RECORD_ANCHOR,
        _RECORD_ANCHOR + f"        {_BESIDE.format(name='MEMORY.hex')}"
        f".write_text({_TEXT}.encode().hex())\n")
    assert _outside_names(problems) == {"MEMORY.hex"}, problems

    # 4. Written, then deleted in normal flow: a crash between the two
    #    leaves it behind.
    problems = await _violations(
        tmp_path / "4", "explanation_store.py", _RECORD_ANCHOR,
        _RECORD_ANCHOR
        + f"        {_BESIDE.format(name='MEMORY.md')}.write_text({_TEXT})\n"
        f"        {_BESIDE.format(name='MEMORY.md')}.unlink()\n")
    assert _outside_names(problems) == {"MEMORY.md"}, problems

    # 5. The same constructor, but the record written beside its directory.
    problems = await _violations(
        tmp_path / "5", "explanation_store.py",
        'return self._root / f"{correlation_id}.json"',
        'return self._root.parent / f"{correlation_id}.json"')
    assert any(re.fullmatch(
        r"explanation_store\.py:\d+ writes /data/a1b2c3d4\.json "
        r"\(os\.rename\): .*, outside row 'explanations'", p)
        for p in problems), problems

    # 6. A persisting site no run makes write.
    problems = await _violations(
        tmp_path / "6", "explanation_store.py", _RECORD_ANCHOR,
        _RECORD_ANCHOR + "        if record.kind == 'never-run':\n"
        f"            {_BESIDE.format(name='NEVER.md')}.write_text({_TEXT})\n")
    assert any("persists memory text to \"self._root.parent / 'NEVER.md'\", "
               "and no run of explanation_store.py:ExplanationStore.record "
               "made it write" in p for p in problems), problems

    # 7. A write that carries no memory but was never declared.
    problems = await _violations(
        tmp_path / "7", "explanation_store.py", _RECORD_ANCHOR,
        _RECORD_ANCHOR
        + f"        {_BESIDE.format(name='NOTES.txt')}.write_text('x')\n")
    assert any(re.fullmatch(
        r"explanation_store\.py:\d+ writes /data/NOTES\.txt \(open\): a file "
        r"it was never declared to write, outside row 'explanations'", p)
        for p in problems), problems

    # 8. Memory text written into a file the producer may write for other
    #    reasons (the indexer's own opening of memory.db): a memory-bearing
    #    site is judged against its row alone.
    anchor = ('            conn.execute("UPDATE indexed SET through = ? '
              'WHERE id = 1",\n')
    problems = await _violations(
        tmp_path / "8", "memory/search.py", anchor,
        "            open(os.path.join(store.directory, 'memory.db'), 'a')"
        ".write(''.join(map(str, rows)))\n" + anchor)
    assert any(re.fullmatch(
        r"memory/search\.py:\d+ writes /data/memory/memory\.db \(open\): "
        r"memory text from catch_up .*, outside row 'search index'", p)
        for p in problems), problems

    # 9. The served-view renderer persisting the view outside its row.
    problems = await _violations(
        tmp_path / "9", "memory/view.py",
        "            block_hash=view.block_hash))\n    return Served(view)\n",
        "            block_hash=view.block_hash))\n"
        "    open(store.directory + '/../view.txt', 'w').write(view.text)\n"
        "    return Served(view)\n")
    assert any(re.fullmatch(
        r"memory/view\.py:\d+ writes /data/view\.txt \(open\): memory text "
        r"from serve .*, outside row 'memory\.db'", p) for p in problems), \
        problems

    # 10. The search index built outside its row.
    problems = await _violations(
        tmp_path / "10", "memory/search.py",
        "return os.path.join(store.directory, SEARCH_DB_NAME)",
        'return os.path.join(store.directory, "..", "index.db")')
    assert any(re.fullmatch(
        r"memory/search\.py:\d+ writes /data/index\.db "
        r"\(sqlite3\.connect\): a file it was never declared to write, "
        r"outside row 'search index'", p) for p in problems), problems

    # 11. A launcher configuring a transcript root with no row.
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
        staging=True)
    assert len(problems) == 1 and re.fullmatch(
        r"session_registry\.py:SessionRegistry\._write staged memory text at "
        r"/data/\.[^/]+\.tmp, which row 'session pointers' does not declare",
        problems[0]), problems

    # 2. A staging file with its declared name, in another directory.
    problems = await _violations(
        tmp_path / "2", "explanation_store.py",
        'temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")',
        'temporary = self._root.parent / f"{path.stem}.{uuid.uuid4().hex}.tmp"',
        staging=True)
    assert len(problems) == 1 and re.fullmatch(
        r"explanation_store\.py:ExplanationStore\.record staged memory text "
        r"at /data/a1b2c3d4\.[0-9a-f]{32}\.tmp, which row 'explanations' "
        r"does not declare", problems[0]), problems
