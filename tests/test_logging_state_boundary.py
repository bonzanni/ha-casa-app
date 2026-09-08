"""#911 defect A — a test gives back the process-global logging state it changed.

``log_cid.install_logging`` writes four pieces of process-global state (a
``_casa_owned`` root handler, the ``LogRecord`` factory, the root level, and the
``httpx``/``opentelemetry`` levels) and has no uninstall. ``casa_core.main()``
calls it as its FIRST statement, so a test that runs the real boot function —
``tests/test_ingress_identity_boot_check.py`` does, deliberately and
unconditionally, as a declared INV-HTTP-005 binding — leaves all four behind for
whatever test the scheduler runs next on that worker.

That is what made ``make test-unit-serial`` red while ``make test-unit`` stayed
green: not isolation, but the accident that ``--dist loadfile`` happened to
schedule the polluter last. ``--dist load`` and ``--dist loadgroup`` were red on
the same three files at any worker count.

The tests here pin the containment boundary (``casa_logging_containment``, autouse
from ``tests/conftest.py``) and the faithfulness of ``logging_state``'s snapshot
and restore. The GUARD's own arms are pinned next door in
``tests/test_logging_state_hygiene.py``, which is where the accusing fixture is
imported; this module deliberately does NOT import it, because one of the tests
below runs the real boot function and would be accused by it — correctly, since
inside the guard that test really does leave residue.
"""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from logging_state import (
    PINNED_LOGGERS,
    casa_handlers,
    residue,
    restore,
    snapshot,
)

TESTS_DIR = Path(__file__).resolve().parent

#: A baseline distinct from BOTH what ``install_logging`` leaves (root INFO/DEBUG,
#: both pinned loggers WARNING) and the interpreter default (root WARNING, pinned
#: NOTSET), so a restored value cannot coincide with an unrestored one.
BASELINE_ROOT = logging.ERROR
BASELINE_PINNED = {"httpx": logging.DEBUG, "opentelemetry": logging.CRITICAL}


def _casa_handler() -> logging.Handler:
    h = logging.StreamHandler(io.StringIO())
    h._casa_owned = True                       # type: ignore[attr-defined]
    return h


@pytest.fixture(scope="module")
def logging_baseline():
    """A distinctive module-scoped baseline, established OUTSIDE the
    function-scoped containment fixture, so each test in this module is
    contained back to exactly it."""
    before = snapshot()
    root = logging.getLogger()
    root.setLevel(BASELINE_ROOT)
    for name, level in BASELINE_PINNED.items():
        logging.getLogger(name).setLevel(level)
    try:
        yield snapshot()
    finally:
        restore(before)


class TestResidueAttributesHandlersToTheTestThatChangedThem:
    """The handler arm is a DIFFERENCE against ``before``, like its three
    siblings — not an absolute count. Four cases, because a one-sided test
    passes both for an arm that never fires and for one that always fires."""

    def test_an_inherited_handler_untouched_is_not_residue(self):
        root = logging.getLogger()
        h = _casa_handler()
        root.addHandler(h)
        try:
            before = snapshot()
            assert residue(before) == []
        finally:
            root.removeHandler(h)

    def test_an_added_handler_is_residue(self):
        root = logging.getLogger()
        before = snapshot()
        h = _casa_handler()
        root.addHandler(h)
        try:
            assert len(residue(before)) == 1
        finally:
            root.removeHandler(h)

    def test_a_removed_handler_is_residue(self):
        root = logging.getLogger()
        h = _casa_handler()
        root.addHandler(h)
        try:
            before = snapshot()
            root.removeHandler(h)
            assert len(residue(before)) == 1
        finally:
            if h in root.handlers:
                root.removeHandler(h)

    def test_a_same_count_replacement_is_residue(self):
        root = logging.getLogger()
        h1 = _casa_handler()
        root.addHandler(h1)
        h2 = _casa_handler()
        try:
            before = snapshot()
            root.removeHandler(h1)
            root.addHandler(h2)
            assert len(casa_handlers()) == 1
            assert len(residue(before)) == 1
        finally:
            for h in (h1, h2):
                if h in root.handlers:
                    root.removeHandler(h)


class TestRestoreIsFaithfulToTheSnapshot:
    """Restore-to-snapshot, never force-a-default: a Casa handler that was there
    comes back, at its position, and nothing that was not Casa's is touched."""

    def test_a_removed_snapshotted_handler_is_reinstated_in_place(self):
        root = logging.getLogger()
        h1, h2 = _casa_handler(), _casa_handler()
        foreign = logging.StreamHandler(io.StringIO())
        added = [h1, foreign, h2]
        for h in added:
            root.addHandler(h)
        base_index = root.handlers.index(h1)
        try:
            before = snapshot()
            root.removeHandler(h1)
            root.removeHandler(h2)
            root.addHandler(_casa_handler())
            restore(before)
            assert [h for h in root.handlers if getattr(h, "_casa_owned", False)] == [h1, h2]
            assert root.handlers[base_index] is h1
            assert root.handlers[base_index + 1] is foreign
            assert root.handlers[base_index + 2] is h2
        finally:
            for h in list(root.handlers):
                if h in added or getattr(h, "_casa_owned", False):
                    root.removeHandler(h)

    def test_restore_leaves_foreign_handlers_and_root_filters_alone(self):
        root = logging.getLogger()
        foreign = logging.StreamHandler(io.StringIO())
        marked = logging.Filter()
        marked._casa_owned = True              # type: ignore[attr-defined]
        root.addHandler(foreign)
        root.addFilter(marked)
        try:
            before = snapshot()
            restore(before)
            assert foreign in root.handlers
            assert marked in root.filters
        finally:
            root.removeHandler(foreign)
            root.removeFilter(marked)

    def test_restore_returns_a_non_default_factory_and_levels(self):
        root = logging.getLogger()
        before = snapshot()

        def other_factory(*a, **k):
            return logging.LogRecord(*a, **k)

        try:
            logging.setLogRecordFactory(other_factory)
            baseline = snapshot()
            root.setLevel(logging.CRITICAL)
            logging.getLogger("httpx").setLevel(logging.NOTSET)
            restore(baseline)
            assert logging.getLogRecordFactory() is other_factory
            assert root.level == baseline.root_level
            assert logging.getLogger("httpx").level == dict(baseline.pinned)["httpx"]
        finally:
            restore(before)


def test_the_containment_helper_imports_no_application_module():
    """``tests/conftest.py``'s containment fixture must survive ``qa.yml``'s
    root lane, a bare ``python:3.11-slim`` where importing an application module
    raises. It does so by construction rather than by an ``ImportError`` guard —
    swallowing that error would silently disable containment — so the property
    that makes it safe is pinned here: ``logging_state`` imports only the
    standard library and pytest."""
    tree = ast.parse((TESTS_DIR / "logging_state.py").read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots - {"pytest"} <= set(sys.stdlib_module_names), sorted(roots)


def test_a_leaking_test_is_reported_against_its_own_node_id(tmp_path):
    """Containment must not erase the guard's evidence before the guard has
    computed it. Measured through pytest's own REPORTS, in a child process that
    loads the real ``logging_state`` as a plugin — so both the containment
    fixture and the accusing guard are the shipped objects, wired by pytest's
    own fixture resolution rather than by this test's beliefs about ordering.

    Written into ``tmp_path`` and never under the tree: the candidate gate runs
    on a READ-ONLY materialization."""
    child = tmp_path / "test_child_leak.py"
    child.write_text(
        "import io, logging\n"
        "\n"
        "def test_leaker():\n"
        "    logging.getLogger().setLevel(logging.CRITICAL)\n"
        "\n"
        "def test_clean():\n"
        "    logging.getLogger('casa.child').debug('nothing global here')\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(TESTS_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "logging_state",
         "-p", "no:cacheprovider", "-p", "no:randomly", "--tb=line", "-q",
         str(child)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    assert "2 passed" in out, out
    errors = [ln for ln in out.splitlines() if "left Casa logging residue" in ln]
    assert len(errors) == 1, out
    assert "root logger level" in errors[0], errors[0]
    assert "test_leaker" in out and "ERROR" in out, out
    assert "test_clean" not in "".join(
        ln for ln in out.splitlines() if "ERROR" in ln), out


class TestContainmentAcrossTheBoundary:
    """Two ORDERED items: the first reaches the installer transitively through
    the real ``casa_core.main()``; the second observes the state that survived
    the boundary between them. Only the containment fixture stands between.

    LAST in the file on purpose: on the PRE-FIX tree this pair leaks, and a
    leaked handler would make the absolute-count assertions above pass for the
    wrong reason — the failure mode #911 is about, reproduced inside the red
    case itself."""

    def test_a_runs_the_real_boot_function(self, logging_baseline, monkeypatch):
        import casa_core

        class _Sentinel(Exception):
            pass

        calls: list[str] = []

        def fake_validate():
            calls.append("validated")
            raise _Sentinel("stop here")

        monkeypatch.setattr(
            casa_core, "validate_ingress_identity_table", fake_validate)
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        with pytest.raises(_Sentinel):
            asyncio.run(casa_core.main())
        # The polluter really did pollute — otherwise the next test would pass
        # for the wrong reason.
        assert calls == ["validated"]
        assert len(casa_handlers()) == 1
        assert getattr(logging.getLogRecordFactory(), "_casa_owned", False) is True

    def test_b_observes_the_state_the_boundary_should_have_restored(
            self, logging_baseline):
        assert casa_handlers() == []
        assert logging.getLogRecordFactory() is logging_baseline.factory
        assert logging.getLogger().level == BASELINE_ROOT
        assert [logging.getLogger(n).level for n in PINNED_LOGGERS] == [
            BASELINE_PINNED[n] for n in PINNED_LOGGERS]
