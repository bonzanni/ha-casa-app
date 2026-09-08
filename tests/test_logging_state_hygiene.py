"""#898 — the residue guard covers every module that calls the installer, and
the guard's own four arms each detect the effect they name.

`log_cid.install_logging` is process-global (a `_casa_owned` root handler, the
root level, a wrapped LogRecord factory, and the `httpx`/`opentelemetry`
levels). `tests/logging_state.py`'s autouse `casa_logging_guard` fails a test
that leaves any of those behind — but autouse applies only where the fixture is
imported, so a NEW test module that starts calling the installer would silently
re-open the abort. The first test here refuses that.
"""

from __future__ import annotations

import ast
import inspect
import io
import logging
from pathlib import Path

import pytest

from log_cid import install_logging
from logging_state import (  # noqa: F401 — autouse where imported
    casa_logging_guard,
    casa_logging_restored,
    residue,
    restore,
    snapshot,
)

TESTS_DIR = Path(__file__).resolve().parent
GUARD = "casa_logging_guard"


def _calls_installer(tree: ast.AST) -> bool:
    """A real ``install_logging(...)`` call node — not the word in a comment or
    a docstring, which is all `tests/test_run_script_env.py:59` has."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name == "install_logging":
                return True
    return False


def _imports_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "logging_state":
            if any(alias.name == GUARD for alias in node.names):
                return True
    return False


def test_every_module_calling_install_logging_imports_the_guard():
    callers, unguarded = [], []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _calls_installer(tree):
            continue
        callers.append(path.name)
        if not _imports_guard(tree):
            unguarded.append(path.name)
    assert unguarded == [], (
        f"{unguarded} call log_cid.install_logging without importing "
        f"logging_state.{GUARD}; install_logging is process-global and an "
        f"unguarded caller re-opens #898")
    # The count is asserted after the emptiness, and not instead of it: a scan
    # that silently stopped finding callers would otherwise pass by finding
    # nothing, and a scan that found a new one would report the wrong thing.
    assert len(callers) == 4, callers


class TestGuardArms:
    """Each arm of ``residue`` detects the effect it names, and ``restore``
    clears it. Without this, a guard whose arms were all dead would still make
    every guarded test pass."""

    def test_installer_leaves_all_four_effects(self):
        before = snapshot()
        try:
            install_logging(level=logging.DEBUG)
            left = residue(before)
        finally:
            restore(before)
        assert len(left) == 5, left        # handler, factory, root, httpx, otel
        assert any("_casa_owned handler" in r for r in left)
        assert any("LogRecord factory" in r for r in left)
        assert any("root logger level" in r for r in left)
        assert any("'httpx'" in r for r in left)
        assert any("'opentelemetry'" in r for r in left)

    def test_restore_clears_every_effect(self):
        before = snapshot()
        with casa_logging_restored():
            install_logging(level=logging.DEBUG)
        assert residue(before) == []

    def test_a_clean_block_has_no_residue(self):
        before = snapshot()
        logging.getLogger("casa.test").debug("nothing global here")
        assert residue(before) == []


class TestContainmentNestsOutsideTheGuard:
    """#911: ``tests/conftest.py`` makes ``casa_logging_containment`` autouse for
    every test, so a test that reaches ``install_logging`` transitively — through
    the real ``casa_core.main()``, which no syntactic scan can see — gives the
    state back at its own boundary.

    Two restorers of the same process-global state now sit side by side, and
    their ORDER is the whole risk: the guard must compute residue and ASSERT
    while containment is still open. Reverse them and the guard is retired in
    silence, which is the one outcome #898 exists to prevent. The nesting is
    therefore declared (the guard takes the containment fixture as a parameter)
    rather than inherited from pytest's conftest-before-module ordering, and both
    halves of that are pinned here."""

    def test_the_guard_declares_containment_as_a_dependency(self):
        params = inspect.signature(casa_logging_guard.__wrapped__).parameters
        assert "casa_logging_containment" in params, sorted(params)

    def test_containment_is_set_up_before_the_guard(self, request):
        # Resolution order IS setup order, and teardown is its reverse — so this
        # is the property, read out of pytest's own fixture closure rather than
        # out of a belief about where conftest sits relative to a module.
        names = list(request.fixturenames)
        assert "casa_logging_containment" in names, names
        assert names.index("casa_logging_containment") < names.index(
            "casa_logging_guard"), names

    def test_the_guard_still_accuses_a_genuine_handler_leak(self):
        """Prohibition against blinding the guard, as an executable test rather
        than a promise: drive the guard's own generator around a body that leaks
        one ``_casa_owned`` handler and require the accusation. Blind the handler
        arm in ``residue`` and this test goes green — which is what makes it
        worth having."""
        guard = casa_logging_guard.__wrapped__
        # Its parameters are fixtures this call does not need; the count is read
        # from the signature so the test does not itself assume the dependency
        # it is not the one pinning.
        gen = guard(*[None] * len(inspect.signature(guard).parameters))
        next(gen)
        root = logging.getLogger()
        leaked = logging.StreamHandler(io.StringIO())
        leaked._casa_owned = True              # type: ignore[attr-defined]
        root.addHandler(leaked)
        with pytest.raises(AssertionError) as excinfo:
            next(gen, None)
        assert "_casa_owned handler" in str(excinfo.value)
        assert leaked not in root.handlers     # the guard restored as it accused
