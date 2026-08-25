#!/usr/bin/env python3
"""The code-derived coverage ledger.

The corpus can only claim completeness against a surface list that comes from the code
itself — a hand-maintained list rots the day after it is written. This script enumerates,
mechanically:

* every ``.py`` under ``casa/rootfs/opt/casa/`` (no size floor),
* every ``options:`` / ``schema:`` key in ``casa/config.yaml``,
* every s6 unit directory under ``casa/rootfs/etc/s6-overlay/s6-rc.d/``,
* every tool in ``tools.py``'s ``CASA_TOOLS`` tuple,
* every HTTP route registration (``add_get``/``add_post``/``add_route``/``add_routes``
  call sites, by AST, so comments and docstrings cannot fake one),
* every environment variable read by literal name (direct ``os.environ``/``os.getenv``,
  ``_env_*`` wrapper helpers, and names bound from ``os.environ``),
* every s6 boot script, every ``defaults/schema/*.json`` file, and the Dockerfile.

``docs/coverage.yaml`` must map every enumerated item to the corpus document that covers
it, or exclude it with a one-line reason. The check is bidirectional, like the manifest:
an enumerated item absent from the ledger fails, a ledger item no longer enumerated
fails, and a ``doc:`` the manifest does not know fails.

Usage:
    python3 scripts/coverage_ledger.py enumerate [repo_root]
    python3 scripts/coverage_ledger.py check [repo_root]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

CODE_ROOT = "casa/rootfs/opt/casa"
S6_ROOT = "casa/rootfs/etc/s6-overlay/s6-rc.d"
SCRIPTS_ROOT = "casa/rootfs/etc/s6-overlay/scripts"
SCHEMA_ROOT = "casa/rootfs/opt/casa/defaults/schema"
DOCKERFILE = "casa/Dockerfile"
CONFIG_YAML = "casa/config.yaml"
TOOLS_MODULE = "tools.py"
# Every module counts — a size floor made ~40 small modules invisible to the
# ledger, and small is not the same as uninteresting (webhook_auth.py is tiny).
MIN_MODULE_LINES = 0

# aiohttp registration spellings. ``add_route(method, path)`` carries its method as the
# first argument; ``add_routes([web.get(path, h), …])`` nests them in a list.
DIRECT_METHODS = {"add_get": "GET", "add_post": "POST", "add_put": "PUT",
                  "add_delete": "DELETE", "add_patch": "PATCH", "add_head": "HEAD"}
ROUTEDEF_NAMES = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
                  "patch": "PATCH", "head": "HEAD", "route": "ROUTE"}


def _expr_text(node: ast.AST) -> str:
    """A path literal yields its value; anything else yields its source text — a dynamic
    path is still a surface, and skipping it would let a whole route family go
    unledgered."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node)


def enumerate_modules(repo_root: Path) -> list[str]:
    root = repo_root / CODE_ROOT
    out = []
    for path in sorted(root.rglob("*.py")):
        try:
            loc = len(path.read_text(errors="replace").splitlines())
        except OSError:
            continue
        if loc >= MIN_MODULE_LINES:
            out.append(str(path.relative_to(repo_root)))
    return out


def enumerate_options(repo_root: Path) -> list[str]:
    try:
        data = yaml.safe_load((repo_root / CONFIG_YAML).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    keys: set[str] = set()
    for section in ("options", "schema"):
        value = data.get(section)
        if isinstance(value, dict):
            keys.update(value)
    return [f"option:{key}" for key in sorted(keys)]


def enumerate_s6(repo_root: Path) -> list[str]:
    root = repo_root / S6_ROOT
    if not root.is_dir():
        return []
    return [f"s6:{p.name}" for p in sorted(root.iterdir()) if p.is_dir()]


def enumerate_tools(repo_root: Path) -> list[str]:
    """The identifiers in the CASA_TOOLS tuple. They are bare function names, not
    ``name=`` keywords — read from the AST, so a commented-out entry does not count."""
    path = repo_root / CODE_ROOT / TOOLS_MODULE
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        if not any(t.id == "CASA_TOOLS" for t in targets):
            continue
        value = node.value
        if not isinstance(value, ast.Tuple):
            return []
        names = []
        for element in value.elts:
            if isinstance(element, ast.Name):
                names.append(f"tool:{element.id}")
            else:
                names.append(f"tool:{ast.unparse(element)}")
        return sorted(set(names))
    return []


def enumerate_routes(repo_root: Path) -> list[str]:
    root = repo_root / CODE_ROOT
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(repo_root))
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name = node.func.attr
            if name in DIRECT_METHODS and node.args:
                found.add(f"route:{rel}:{DIRECT_METHODS[name]}:{_expr_text(node.args[0])}")
            elif name == "add_route" and len(node.args) >= 2:
                method = _expr_text(node.args[0]).upper()
                found.add(f"route:{rel}:{method}:{_expr_text(node.args[1])}")
            elif name == "add_routes" and node.args:
                container = node.args[0]
                elements = container.elts if isinstance(container, (ast.List, ast.Tuple)) else []
                for element in elements:
                    if (
                        isinstance(element, ast.Call)
                        and isinstance(element.func, ast.Attribute)
                        and element.func.attr in ROUTEDEF_NAMES
                        and element.args
                    ):
                        method = ROUTEDEF_NAMES[element.func.attr]
                        found.add(f"route:{rel}:{method}:{_expr_text(element.args[0])}")
                    else:
                        found.add(f"route:{rel}:?:{ast.unparse(element)}")
    return sorted(found)


def _walk_scope(scope: ast.AST):
    """Walk a scope's own statements WITHOUT descending into nested function
    definitions — each function is analysed as its own scope, so an inner
    binding must not leak outward and an inner read must not be judged by an
    outer scope's bindings."""
    stack = list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A nested function is its own scope: yield the definition node
            # itself but never its interior.
            yield node
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _is_os_environ(node: ast.AST) -> bool:
    """Exactly the ``os.environ`` attribute chain — a decoy object whose
    attribute happens to be named ``environ`` must not enumerate."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def enumerate_env_reads(repo_root: Path) -> list[str]:
    """Every environment variable the code reads by literal name, by AST —
    ``os.environ.get("X")``, ``os.environ["X"]``, ``os.getenv("X")``, and the
    project's ``_env_*`` wrapper helpers called with a literal first argument.
    Env vars are the classic undocumented surface: a tunable nobody wrote
    down."""
    root = repo_root / CODE_ROOT
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        # Names bound to os.environ, tracked PER FUNCTION SCOPE — a parameter
        # defaulted to it, a direct alias (`env = os.environ`), or the
        # self-referential rebind (`env = env if … else os.environ`). Literal
        # .get()/[] reads through such a name are env reads too. `raw =
        # os.environ.get(…)` binds a VALUE, not the mapping, and is not
        # tracked; nor does one function's `env` contaminate another's. Reads
        # through further indirection — a closure argument, or a mapping passed
        # in at the call site — are not traced; the option-key enumeration
        # backstops option-derived variables.
        def _scope_env_names(scope: ast.AST) -> set[str]:
            found: set[str] = set()
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = scope.args
                positional = args.posonlyargs + args.args
                for arg, default in zip(
                    positional[len(positional) - len(args.defaults):],
                    args.defaults,
                ):
                    if any(_is_os_environ(n) for n in ast.walk(default)):
                        found.add(arg.arg)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                    if default is not None and any(
                        _is_os_environ(n) for n in ast.walk(default)
                    ):
                        found.add(arg.arg)
            for node in _walk_scope(scope):
                if True:
                    if isinstance(node, ast.Assign):
                        value_names = {
                            n.id for n in ast.walk(node.value)
                            if isinstance(n, ast.Name)
                        }
                        is_alias = _is_os_environ(node.value)
                        for target in node.targets:
                            if isinstance(target, ast.Name) and (
                                is_alias
                                or (
                                    target.id in value_names
                                    and any(
                                        _is_os_environ(n)
                                        for n in ast.walk(node.value)
                                    )
                                )
                            ):
                                found.add(target.id)
            return found

        scopes = [tree] + [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        module_env = _scope_env_names(tree)

        # Module-level `NAME = "literal"` string constants (e.g.
        # `SPOOL_ROOT_ENV = "CASA_EVENT_SPOOL_ROOT"`) — a call site that
        # passes the CONSTANT rather than the literal itself
        # (`os.environ.get(SPOOL_ROOT_ENV)`) would otherwise escape this
        # scanner entirely, since only `ast.Constant` first-arguments are
        # recognized below. Single simple top-level assignment only — no
        # attempt to trace re-assignment, conditional binding, or
        # anything past one level of indirection.
        module_str_consts: dict = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                module_str_consts[node.targets[0].id] = node.value.value

        def _literal_or_const(arg: ast.AST) -> "str | None":
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
            if isinstance(arg, ast.Name) and arg.id in module_str_consts:
                return module_str_consts[arg.id]
            return None

        for scope in scopes:
            env_names = (
                module_env if scope is tree
                else module_env | _scope_env_names(scope)
            )

            def _is_env_base(base: ast.AST) -> bool:
                return _is_os_environ(base) or (
                    isinstance(base, ast.Name) and base.id in env_names
                )

            for node in _walk_scope(scope):
                if isinstance(node, ast.Call):
                    name = node.args and _literal_or_const(node.args[0])
                    if not name:
                        continue
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        is_env_get = (
                            func.attr == "get" and _is_env_base(func.value)
                        )
                        is_getenv = (
                            func.attr == "getenv"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "os"
                        )
                        if is_env_get or is_getenv:
                            names.add(name)
                    elif isinstance(func, ast.Name) and func.id.startswith("_env_"):
                        # Local wrappers (_env_int, _env_int_or, _env_float_or,
                        # …) take the variable name as their literal first
                        # argument.
                        names.add(name)
                elif isinstance(node, ast.Subscript):
                    if _is_env_base(node.value):
                        name = _literal_or_const(node.slice)
                        if name:
                            names.add(name)
    return [f"env:{name}" for name in sorted(names)]


def enumerate_scripts(repo_root: Path) -> list[str]:
    root = repo_root / SCRIPTS_ROOT
    if not root.is_dir():
        return []
    return [f"script:{p.name}" for p in sorted(root.iterdir()) if p.is_file()]


def enumerate_schemas(repo_root: Path) -> list[str]:
    root = repo_root / SCHEMA_ROOT
    if not root.is_dir():
        return []
    return [f"schema:{p.name}" for p in sorted(root.iterdir()) if p.is_file()]


def enumerate_dockerfile(repo_root: Path) -> list[str]:
    return [DOCKERFILE] if (repo_root / DOCKERFILE).is_file() else []


def enumerate_items(repo_root: Path) -> list[str]:
    return (
        enumerate_modules(repo_root)
        + enumerate_options(repo_root)
        + enumerate_s6(repo_root)
        + enumerate_tools(repo_root)
        + enumerate_routes(repo_root)
        + enumerate_env_reads(repo_root)
        + enumerate_scripts(repo_root)
        + enumerate_schemas(repo_root)
        + enumerate_dockerfile(repo_root)
    )


# --- the check ------------------------------------------------------------------------

def _load_ledger(repo_root: Path) -> tuple[list[dict], list[str]]:
    path = repo_root / "docs" / "coverage.yaml"
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError:
        return [], ["docs/coverage.yaml is missing — the coverage ledger is mandatory"]
    except yaml.YAMLError as exc:
        return [], [f"docs/coverage.yaml is not valid YAML: {exc}"]
    if not isinstance(raw, list):
        return [], ["docs/coverage.yaml must be a list of entries"]
    entries, problems = [], []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("item"), str):
            problems.append(f"coverage entry {index} is not a mapping with a string `item`")
            continue
        entries.append(entry)
    return entries, problems


def _manifest_docs(repo_root: Path) -> set[str]:
    docs_dir = repo_root / "docs"
    # #367: the manifest shards at its index ceiling — read the root plus every
    # docs/manifest.d/*.yaml shard, mirroring verify_docs._manifest_files.
    sources = [docs_dir / "manifest.yaml"] + sorted((docs_dir / "manifest.d").glob("*.yaml"))
    out: set[str] = set()
    for source in sources:
        try:
            raw = yaml.safe_load(source.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, list):
            continue
        out |= {e["doc"] for e in raw if isinstance(e, dict) and isinstance(e.get("doc"), str)}
    return out


_STRICT_LOADER = None


def _strict_loader():
    """verify_docs._DuplicateKeyLoader, loaded from the sibling file by path.

    ONE loader on both sides (#717 review round 1, Terra S1): PyYAML's
    ``safe_load`` silently keeps the LAST of a duplicate key, so a manifest
    entry carrying two ``covers`` blocks would hand this ledger a different
    claimant map from the one ``verify_docs`` parses strictly for the impact
    rule — and the cross-check below would fail open on exactly that shape.
    Loaded by file location because this script runs standalone, as a
    subprocess of verify_docs, and via file-location import in the tests — no
    package context is guaranteed in any of them.
    """
    global _STRICT_LOADER
    if _STRICT_LOADER is None:
        import importlib.util

        path = Path(__file__).resolve().parent / "verify_docs.py"
        spec = importlib.util.spec_from_file_location("casa_verify_docs_for_ledger", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _STRICT_LOADER = mod._DuplicateKeyLoader
    return _STRICT_LOADER


def _covers_claimants(repo_root: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Path -> documents whose manifest ``covers`` anchors claim it, plus problems.

    Resolution mirrors ``verify_docs._claimants``: an anchor claims its PATH
    component (``path::Symbol`` -> ``path``), which is also how
    ``verify_docs --impact`` names documents for a changed file — the point of
    the cross-check in ``check`` is that this ledger and that guard cannot
    name different owners for the same path. Read from the root manifest plus
    every docs/manifest.d/*.yaml shard, like ``_manifest_docs`` — but with the
    strict duplicate-key-rejecting loader, and a parse failure PROPAGATES as a
    ledger problem rather than silently yielding no claims: a claimant map the
    ledger cannot trust must fail the check, not weaken it.
    """
    problems: list[str] = []
    try:
        loader = _strict_loader()
    except Exception as exc:  # noqa: BLE001 — any failure here disables the cross-check
        return {}, [
            f"coverage: cannot load the strict manifest loader from verify_docs.py "
            f"({exc}) — the covers cross-check cannot run"
        ]
    docs_dir = repo_root / "docs"
    sources = [docs_dir / "manifest.yaml"] + sorted((docs_dir / "manifest.d").glob("*.yaml"))
    out: dict[str, set[str]] = {}
    for source in sources:
        try:
            text = source.read_text()
        except OSError:
            continue  # a missing root manifest already reds check() via _manifest_docs
        try:
            raw = yaml.load(text, loader)
        except yaml.YAMLError as exc:
            problems.append(
                f"coverage: {source.name} fails the strict manifest parse ({exc}) — "
                f"the covers cross-check cannot trust its claims"
            )
            continue
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("doc"), str):
                continue
            for cov in entry.get("covers") or []:
                if isinstance(cov, str):
                    out.setdefault(cov.split("::", 1)[0], set()).add(entry["doc"])
    return out, problems


def check(repo_root: Path) -> list[str]:
    """Return every coverage problem. Empty list means every surface is accounted for."""
    entries, problems = _load_ledger(repo_root)
    if problems and not entries:
        return problems
    manifest = _manifest_docs(repo_root)
    claimants, claim_problems = _covers_claimants(repo_root)
    problems.extend(claim_problems)
    enumerated = set(enumerate_items(repo_root))

    seen: set[str] = set()
    for entry in entries:
        item = entry["item"]
        if item in seen:
            problems.append(f"coverage: {item!r} is listed twice")
        seen.add(item)
        doc, excluded = entry.get("doc"), entry.get("excluded")
        if (doc is None) == (excluded is None):
            problems.append(
                f"coverage: {item!r} must carry exactly one of `doc` or `excluded`"
            )
            continue
        if doc is not None and doc not in manifest:
            problems.append(
                f"coverage: {item!r} is assigned to {doc!r}, which is not in the manifest"
            )
        # #717 (ruled: option B, fatal): where the two ownership maps OVERLAP
        # they must agree. An item some document's `covers` claims must be
        # assigned to one of its claimants — otherwise `verify_docs --impact`
        # names one owner while this ledger asserts another. Items nothing
        # claims are deliberately not judged here: widening the guard's
        # coverage is a separate decision, and namespaced items (option:,
        # s6:, tool:, route:) never key a covers path at all.
        if doc is not None and item in claimants and doc not in claimants[item]:
            problems.append(
                f"coverage: {item!r} is assigned to {doc!r}, but the manifest "
                f"covers claim it for {', '.join(sorted(claimants[item]))} — "
                f"the two ownership maps disagree"
            )
        if excluded is not None and (not isinstance(excluded, str) or not excluded.strip()):
            problems.append(
                f"coverage: {item!r} is excluded without a reason — every exclusion "
                f"states one"
            )

    for item in sorted(enumerated - seen):
        problems.append(
            f"coverage: {item!r} exists in the code but is not in docs/coverage.yaml — "
            f"assign it to a document or exclude it with a reason"
        )
    for item in sorted(seen - enumerated):
        problems.append(
            f"coverage: {item!r} is in the ledger but no longer enumerated from the "
            f"code — remove the stale entry"
        )
    return problems


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("enumerate", "check"):
        print(__doc__)
        return 2
    root = Path(args[1] if len(args) > 1 else ".").resolve()
    if args[0] == "enumerate":
        for item in enumerate_items(root):
            print(item)
        return 0
    problems = check(root)
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        print(f"\n{len(problems)} coverage problem(s).")
        return 1
    print("✓ coverage ledger verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
