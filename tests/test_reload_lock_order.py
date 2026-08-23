"""#706 / INV-CFG-011 — the fenced reload-scope set, pinned against drift.

The behavioural cases live in `test_bundle_reload_fence.py`: they prove that
today's `executors` and `plugin_env` reloads take the plugin-tools lock before
the reload RW lock, at both entry points. They cannot prove the SET stays
right. Two ways it silently stops being right, one arm each:

1. a new (or moved) reload handler acquires `tools._plugin_tools_guard()` while
   its scope is not fenced — it would then run `RW reader -> plugin lock`, the
   inversion, again;
2. a new production caller dispatches a fenced scope WITHOUT going through an
   entry point that fences it — same inversion, reached from somewhere else.

Both arms are static, because the failure they catch is a code path nobody has
written a caller for yet.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The two production reload entry points. Both classify through
# tools._plugin_tools_reload_guard, which is the point: this list is what a
# third entry point would have to be added to, deliberately.
GUARDED_ENTRY_POINTS = {
    ("tools.py", "casa_reload"),
    ("internal_handlers.py", "build_admin_reload_handler"),
}


def _code_root() -> Path:
    import reload as reload_mod
    return Path(reload_mod.__file__).resolve().parent


def _fenced_scopes() -> frozenset:
    import tools as tools_mod
    return tools_mod._PLUGIN_TOOLS_RELOAD_SCOPES


def _takes_the_guard(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else None)
            if name == "_plugin_tools_guard":
                return True
    return False


def test_the_fenced_set_is_the_scopes_whose_handlers_take_the_guard() -> None:
    """Arm 1. Every guard-taking handler in reload.py is registered under a
    fenced scope."""
    tree = ast.parse((_code_root() / "reload.py").read_text())

    registered: dict[str, str] = {}          # function name -> scope
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_handler"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Name)):
            registered[node.args[1].id] = node.args[0].value

    guard_takers = sorted(
        fn.name for fn in tree.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _takes_the_guard(fn)
    )
    assert guard_takers == ["reload_executors", "reload_plugin_env"]

    unfenced = sorted(
        (fn, registered.get(fn)) for fn in guard_takers
        if registered.get(fn) not in _fenced_scopes()
    )
    assert unfenced == [], (
        "a reload handler takes the plugin-tools guard under a scope that the "
        "entry points do not fence — that is the INV-CFG-011 inversion")


def test_no_production_caller_dispatches_a_fenced_scope_outside_an_entry_point() -> None:
    """Arm 2. A literal `dispatch("<fenced scope>")` may appear only inside a
    recognized guarded entry point."""
    fenced = _fenced_scopes()
    offenders: list[tuple[str, str, str]] = []

    for path in sorted(_code_root().glob("*.py")):
        tree = ast.parse(path.read_text())
        for top in tree.body:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(top):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else None)
                if name != "dispatch" or not sub.args:
                    continue
                first = sub.args[0]
                if not (isinstance(first, ast.Constant)
                        and first.value in fenced):
                    continue
                if (path.name, top.name) in GUARDED_ENTRY_POINTS:
                    continue
                offenders.append((path.name, top.name, first.value))

    assert offenders == [], (
        "a production path dispatches a fenced reload scope outside the two "
        "guarded entry points, so nothing acquires the plugin-tools lock "
        "before the reload RW lock on that path (INV-CFG-011)")


@pytest.mark.parametrize("scope", ["full", "executors", "plugin_env"])
def test_the_fenced_set_contains(scope: str) -> None:
    assert scope in _fenced_scopes()


def test_the_fenced_set_is_exactly_three_scopes() -> None:
    """`agent`, `agents`, `policies`, `triggers` and `config_sync` must stay
    OUT: a bundle transaction holds the raw plugin lock and dispatches
    `agent`/`agents`, so fencing those would deadlock it against itself."""
    assert _fenced_scopes() == frozenset({"full", "executors", "plugin_env"})
