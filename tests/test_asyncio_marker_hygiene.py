"""#915: no synchronous test is placed under `pytest.mark.asyncio`.

The rule, exactly. A synchronous test is placed under the marker when either of
two things is true of the SOURCE:

1. the marker is named in a ``pytestmark`` assignment in the test's own MODULE
   body, read flattened through ``if``/``try``/``with``/``for``/``while``/
   ``match`` and never into a nested ``def``/``class``;
2. the marker decorates the test function.

The marker itself is recognised by two spellings and no others: an attribute
``asyncio`` whose owner is an attribute named ``mark`` (so ``pytest.mark.asyncio``
and ``pt.mark.asyncio`` both count, with or without a call), or an attribute
``asyncio`` on a bare name that this module introduced directly in its flattened
suite by ``from pytest import mark`` (plain or aliased) or by an assignment,
annotated or not, whose value is an attribute named ``mark``.

**Two clauses, and that is the whole rule.** It is not a model of pytest's marker
resolution, and the shape of this file is the record of finding that out. Earlier
drafts tried to be one: they worked out what a ``pytestmark`` binding would
evaluate to, they followed base classes through the MRO, they read class-level
``pytestmark`` and class decorators, they honoured ``__test__ = False``. Every one
of those clauses was reported incomplete by a reviewer who reproduced the gap —
an alias of an alias, a base class behind an alias, a base class's decorator, an
annotated ``__test__`` reassignment — and every repair opened the next one,
because pytest's surface is larger than any rule that reads one file. Six such
findings in one mechanism is the point at which the answer is to cut the
mechanism, not sharpen it again, so the clauses that could never be completed are
GONE rather than patched a seventh time.

What that costs, stated rather than discovered:

- A ``pytestmark`` on a CLASS, a marker decorating a class, and a marker
  inherited from a base class are **outside this rule**. It will not accuse them
  and does not claim to.
- ``__test__ = False`` is **not honoured**. A module or class that opts out of
  collection while naming the marker IS accused. That is the conservative
  direction — the guard speaks and a human looks — and no module in this tree
  writes ``__test__`` at all (``grep -rn __test__ tests/ casa/`` is empty).

What it buys is that the two remaining clauses are COMPLETE for what they claim,
and they are exactly the property the change establishes: the change deleted 40
module-level ``pytestmark`` assignments, and all 296 violations it cleared came
from that one source — not one from a class, a base class or a decorator.

`pytest.ini` sets `asyncio_mode = auto`, so pytest-asyncio binds every `async
def` test itself and an explicit `pytest.mark.asyncio` adds nothing to an async
test. On a *synchronous* test the same mark has no consumer at all — nothing in
this repository reads markers, no selection expression names `asyncio` — and its
one remaining effect is pytest-asyncio's "is marked with '@pytest.mark.asyncio'
but it is not an async function" warning. At the commit that introduced this
guard there were 331 such warnings, from 296 synchronous functions in 40
modules, and they were the entire warning output of those 40 files.

The rule is a property of the population, deliberately **not a count**. "Exactly
N files" is a number every new test module moves, and it says nothing about the
module that reintroduces the mark while another one is deleted.

Two more limits, stated for the same reason:

- It says nothing about **async** tests. 148 modules still carry a redundant
  module-level ``asyncio`` mark with no synchronous test to inherit it; that is
  #921's population, this rule passes over it, and a rule that reddened on it
  would be a different rule.
- Collection is read as this repository configures it — ``test*`` functions at
  module level or inside ``Test*`` classes, since ``pytest.ini`` overrides
  neither ``python_functions`` nor ``python_classes``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _suite_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """Every statement BELONGING to this suite, flattened.

    A module body executes its ``if``, ``try``, ``with``, ``for``, ``while`` and
    ``match`` branches in its own namespace, so a ``pytestmark``, a ``class`` or
    a ``def`` written inside one of those belongs to this suite exactly as a
    top-level statement does. A nested ``def``/``class`` body does NOT: names
    bound there belong to that suite.

    There is ONE flattener because there were three, and they disagreed — a
    reviewer reproduced a ``pytestmark`` found inside an ``if`` while a ``def``
    inside the same ``if`` was never discovered. Both walks below start here.
    """
    out: list[ast.stmt] = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list):
                out.extend(_suite_statements(
                    [st for st in inner if isinstance(st, ast.stmt)]))
        for handler in getattr(stmt, "handlers", []) or []:
            out.extend(_suite_statements(handler.body))
        for case in getattr(stmt, "cases", []) or []:
            out.extend(_suite_statements(case.body))
    return out


def _mark_aliases(stmts: list[ast.stmt]) -> frozenset[str]:
    """Names this module introduces DIRECTLY for `pytest.mark`.

    `from pytest import mark` binds `mark`; `... as p` binds `p`; `m =
    pytest.mark` and `m: object = pytest.mark` both bind `m`. A name bound to
    another such name (`n = p`) is NOT followed — that is the governing rule
    below, and following it means tracking assignment chains.
    """
    names = set()
    for stmt in stmts:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "pytest":
            names.update(a.asname or a.name for a in stmt.names
                         if a.name == "mark")
            continue
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        value = getattr(stmt, "value", None)
        if isinstance(value, ast.Attribute) and value.attr == "mark":
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(names)


def _is_asyncio_mark(node: ast.expr, aliases: frozenset[str]) -> bool:
    """Is this expression the asyncio marker?

    **This rule follows no rebinding.** The receiver is matched by its exact
    spelling at the site where it is used: an attribute named ``mark``, or a bare
    name this module introduced directly for `pytest.mark`. `p = pytest.mark`
    then `n = p` is outside the rule, and is pinned below as something it does
    not accuse.
    """
    while isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute) or node.attr != "asyncio":
        return False
    owner = node.value
    if isinstance(owner, ast.Attribute):
        return owner.attr == "mark"
    return isinstance(owner, ast.Name) and owner.id in aliases


def _marks(value: ast.expr) -> list[ast.expr]:
    """The elements of a `pytestmark` assignment, list/tuple or bare."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return list(value.elts)
    return [value]


def _module_pytestmark_is_asyncio(stmts: list[ast.stmt],
                                  aliases: frozenset[str]) -> bool:
    """Does this module assign a `pytestmark` naming the marker?

    ANY such assignment counts. This is syntactic by definition rather than an
    attempt to work out what the binding would evaluate to: a module that names
    the marker and then rebinds ``pytestmark`` to something else IS accused,
    although it marks nothing at collection. That over-report is the price of not
    modelling evaluation, it is pinned below, and no module in this tree pays it.
    """
    for stmt in stmts:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark"
                   for t in targets):
            continue
        value = getattr(stmt, "value", None)
        if value is not None and any(_is_asyncio_mark(m, aliases)
                                     for m in _marks(value)):
            return True
    return False


def marked_sync_tests(source: bytes, filename: str) -> list[str]:
    """Every synchronous test in `source` placed under the asyncio marker.

    Each entry is ``<filename>:<lineno>:<qualname> (<source of the mark>)``, so a
    failure names the file, the function and which of the two clauses placed the
    test under the mark.
    """
    tree = ast.parse(source, filename)
    found: list[str] = []
    module_stmts = _suite_statements(tree.body)
    aliases = _mark_aliases(module_stmts)
    inherited = ("module-level pytestmark"
                 if _module_pytestmark_is_asyncio(module_stmts, aliases)
                 else None)

    def visit(stmts: list[ast.stmt], prefix: str) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                if stmt.name.startswith("Test"):
                    visit(_suite_statements(stmt.body),
                          f"{prefix}{stmt.name}.")
            elif isinstance(stmt, ast.FunctionDef):
                if not stmt.name.startswith("test"):
                    continue
                source_of = inherited
                if any(_is_asyncio_mark(d, aliases)
                       for d in stmt.decorator_list):
                    source_of = "decorator"
                if source_of is not None:
                    found.append(
                        f"{filename}:{stmt.lineno}:{prefix}{stmt.name} "
                        f"({source_of})")

    visit(module_stmts, "")
    return found


class TestNoSyncTestCarriesTheAsyncioMarker:
    """The rule itself, over the whole `tests/` population."""

    def test_no_sync_test_carries_the_asyncio_marker(self) -> None:
        """Counts, not statuses: the number of marked synchronous tests is 0.

        At the commit that introduced this guard the count was 296, across 40
        modules, every one of them inheriting from a module-level `pytestmark`.
        """
        tests_dir = Path(__file__).resolve().parent
        violations: list[str] = []
        scanned = 0
        for path in sorted(tests_dir.glob("test_*.py")):
            scanned += 1
            violations.extend(
                marked_sync_tests(path.read_bytes(), path.name))

        assert scanned > 100, (
            f"only {scanned} test modules were parsed — the glob found almost "
            f"nothing, so a count of 0 violations would prove nothing")
        assert violations == [], (
            f"{len(violations)} synchronous tests carry pytest.mark.asyncio, "
            f"which pytest-asyncio warns about on every one of them:\n"
            + "\n".join(violations))


class TestTheDetectorDetects:
    """Each arm of the detector, positively — a blinded arm must go red here.

    The rule above asserts an EMPTY list, so on a clean tree it passes just as
    well with every arm blinded. These are what make it a guard rather than a
    formality.
    """

    def test_a_module_level_pytestmark_is_a_source(self) -> None:
        src = b"import pytest\npytestmark = pytest.mark.asyncio\ndef test_x():\n    pass\n"
        assert marked_sync_tests(src, "m.py") == [
            "m.py:3:test_x (module-level pytestmark)"]

    def test_a_pytestmark_nested_in_a_statement_is_still_a_source(self) -> None:
        """An assignment inside a module-level ``if`` binds ``pytestmark`` just
        as a top-level one does."""
        src = (b"import pytest\nif True:\n"
               b"    pytestmark = pytest.mark.asyncio\n"
               b"def test_x():\n    pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:4:test_x (module-level pytestmark)"]

    def test_a_pytestmark_inside_a_nested_def_is_not_the_modules(self) -> None:
        """A ``pytestmark`` in a function or class body belongs to that suite."""
        src = (b"import pytest\ndef helper():\n"
               b"    pytestmark = pytest.mark.asyncio\n"
               b"def test_x():\n    pass\n")
        assert marked_sync_tests(src, "m.py") == []

    def test_a_test_defined_inside_a_statement_is_still_discovered(self) -> None:
        """The flattener is shared, so a ``def`` inside a module-level ``if`` is
        discovered wherever a ``pytestmark`` there would be found."""
        src = (b"import pytest\npytestmark = pytest.mark.asyncio\n"
               b"if True:\n    def test_sync():\n        pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:4:test_sync (module-level pytestmark)"]

    def test_a_class_defined_inside_a_statement_is_still_discovered(self) -> None:
        src = (b"import pytest\npytestmark = pytest.mark.asyncio\n"
               b"if True:\n    class TestNested:\n"
               b"        def test_sync(self):\n            pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:5:TestNested.test_sync (module-level pytestmark)"]

    def test_a_decorator_is_a_source(self) -> None:
        # Assembled rather than written out: the repository's pre-commit hook
        # reads a literal "@" followed by a dotted name as an email address.
        decorator = "@" + "pytest.mark.asyncio"
        src = ("import pytest\n" + decorator
               + "\ndef test_x():\n    pass\n").encode()
        assert marked_sync_tests(src, "m.py") == ["m.py:3:test_x (decorator)"]

    def test_an_aliased_mark_decorator_is_a_source(self) -> None:
        """`from pytest import mark as p` then `p.asyncio` reaches collection as
        the real warning."""
        decorator = "@" + "p.asyncio"
        src = ("from pytest import mark as p\n" + decorator
               + "\ndef test_sync():\n    pass\n").encode()
        assert marked_sync_tests(src, "m.py") == ["m.py:3:test_sync (decorator)"]

    def test_an_annotated_alias_binding_is_still_an_alias(self) -> None:
        """`m: object = pytest.mark` binds `m` exactly as `m = pytest.mark`."""
        decorator = "@" + "m.asyncio"
        src = ("import pytest\nm: object = pytest.mark\n" + decorator
               + "\ndef test_sync():\n    pass\n").encode()
        assert marked_sync_tests(src, "m.py") == ["m.py:4:test_sync (decorator)"]

    @pytest.mark.parametrize("assignment", [
        b"pytestmark = pytest.mark.asyncio",
        b"pytestmark = pytest.mark.asyncio()",
        b"pytestmark = [pytest.mark.asyncio, pytest.mark.unit]",
        b"pytestmark = (pytest.mark.unit, pytest.mark.asyncio)",
        b"pytestmark: object = pytest.mark.asyncio",
        b"from pytest import mark\npytestmark = mark.asyncio",
        b"from pytest import mark as p\npytestmark = p.asyncio",
        b"m = pytest.mark\npytestmark = m.asyncio",
        b"m: object = pytest.mark\npytestmark = m.asyncio",
        b"import pytest as pt\npytestmark = pt.mark.asyncio",
        b"pytestmark = [pytest.mark.unit]\npytestmark += [pytest.mark.asyncio]",
        b"pytestmark = [pytest.mark.unit]\npytestmark = [pytest.mark.asyncio]",
        b"pytestmark = pytest.mark.asyncio\npytestmark = pytest.mark.unit",
        b"if True:\n    pytestmark = pytest.mark.asyncio",
        b"try:\n    pytestmark = pytest.mark.asyncio\nexcept Exception:\n    pass",
    ])
    def test_every_spelling_of_the_marker_is_seen(self, assignment: bytes) -> None:
        src = b"import pytest\n" + assignment + b"\ndef test_x():\n    pass\n"
        assert len(marked_sync_tests(src, "m.py")) == 1, assignment

    @pytest.mark.parametrize("source", [
        b"import pytest\npytestmark = pytest.mark.unit\ndef test_x():\n    pass\n",
        b"import pytest\nimport asyncio\npytestmark = pytest.mark.unit\ndef test_x():\n    pass\n",
        b"pytestmark = mark.asyncio\ndef test_x():\n    pass\n",
        b"import pytest\np = pytest.mark\nn = p\npytestmark = n.asyncio\n"
        b"def test_x():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\nasync def test_x():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\ndef helper():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\nclass Helper:\n"
        b"    def test_x(self):\n        pass\n",
    ])
    def test_what_the_rule_does_not_accuse(self, source: bytes) -> None:
        """A `pytest.mark.unit`; a module whose only `asyncio` is the stdlib
        import; a bare `mark` this module never bound; an alias of an alias; an
        async test; a non-test function; and a class pytest does not collect."""
        assert marked_sync_tests(source, "m.py") == []

    @pytest.mark.parametrize("source", [
        b"import pytest\nclass TestA:\n"
        b"    pytestmark = pytest.mark.asyncio\n"
        b"    def test_x(self):\n        pass\n",
        b"import pytest\nclass MarkedBase:\n"
        b"    pytestmark = pytest.mark.asyncio\n"
        b"class TestChild(MarkedBase):\n    def test_x(self):\n        pass\n",
    ])
    def test_what_the_rule_declares_itself_blind_to(self, source: bytes) -> None:
        """The clauses this rule CUT rather than kept sharpening: a `pytestmark`
        on a class, and one inherited from a base class.

        Pinned as blind spots rather than left in prose, because a limit nobody
        can see is a limit nobody can weigh. Six reviewer findings in one
        mechanism — an alias of an alias, a base class behind an alias, a base
        class's decorator, an annotated `__test__` reassignment among them —
        established that a rule reading one file cannot complete pytest's marker
        surface. The two clauses that remain are complete for what they claim,
        and they are exactly the source all 296 cleared violations came from.
        """
        assert marked_sync_tests(source, "m.py") == []

    def test_an_opted_out_module_is_still_accused(self) -> None:
        """``__test__ = False`` is deliberately NOT honoured.

        Honouring it made the guard fall SILENT over modules pytest still
        collects — a reviewer reproduced a conditional one and an annotated
        reassignment — and silence is the wrong failure direction for a guard.
        Accusing an opted-out module is the right one: the guard speaks and a
        human looks. No module in this tree writes ``__test__`` at all.
        """
        src = (b"import pytest\n__test__ = False\n"
               b"pytestmark = pytest.mark.asyncio\ndef test_x():\n    pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:4:test_x (module-level pytestmark)"]
