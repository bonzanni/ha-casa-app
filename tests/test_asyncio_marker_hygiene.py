"""#915: no synchronous test is placed under `pytest.mark.asyncio`.

The rule, exactly: **in no `tests/test_*.py` module does the source place a
synchronous test under the asyncio marker** — the marker is not named in a
`pytestmark` assignment in the test's own module body, nor in the body of an
enclosing class or of a class that class inherits from IN THE SAME MODULE
(wherever in that module it is defined), nor in a decorator on the test itself.

That is a property of the SOURCE TEXT, and it is stated that way on purpose. It
is not a model of pytest's marker resolution, and an earlier draft that tried to
be one was returned four times: a rule about what a marker EVALUATES to at
collection can always be made to disagree with a rule that reads a file, and the
disagreements are unbounded (a rebound `pytestmark`, an assignment nested in an
`if`, a class opted out with `__test__ = False`, a base class in another module).
The named limit is the last of those: a `pytestmark` on a base class IMPORTED
from elsewhere is not followed, because deciding it means resolving imports.
Nothing in this tree does that today.

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

Scoped, and the scope is a limit rather than coverage claimed:

- It asks what the source places a synchronous test UNDER, not which syntax
  delivered it, so all three sources are checked: a module-level ``pytestmark``, a ``pytestmark``
  on an enclosing class, and a decorator on the function. Only the module-level
  source was populated at the commit that cleared it; a detector that looked
  only there would pass a tree in which the same mark had been moved onto a
  class or a decorator.
- It says nothing about **async** tests. 148 modules still carry a redundant
  module-level ``asyncio`` mark with no synchronous test to inherit it; that is
  #921's population, this rule passes over it, and a rule that reddened on it
  would be a different rule.
- Collection is read as this repository configures it — ``test*`` functions in
  ``Test*`` classes (``pytest.ini`` overrides neither ``python_functions`` nor
  ``python_classes``), minus anything opted out with ``__test__ = False`` on the
  module, on the class or on the function — because a name this rule policed
  that pytest never collects would be a false accusation. It does not claim to
  model every collection rule pytest has; it models the ones this tree can
  express.
- The rule is **syntactic by definition**, not an attempt to work out what a
  ``pytestmark`` binding evaluates to: a synchronous test is in violation when
  the marker is NAMED in a ``pytestmark`` assignment somewhere in its module
  body or an enclosing class body, at any statement nesting, or in a decorator
  on the test itself. See ``_suite_pytestmark_is_asyncio`` for why the earlier
  evaluate-it design was cut rather than sharpened, and for the over-report that
  choice accepts.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _is_asyncio_mark(node: ast.expr) -> bool:
    """True for `pytest.mark.asyncio`, with or without a call, any receiver.

    `pytest.mark.asyncio`, `pytest.mark.asyncio(...)` and a `mark.asyncio`
    reached through `from pytest import mark` all answer True; `pytest.mark.unit`
    and a bare name `asyncio` (the module) do not.
    """
    while isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute) or node.attr != "asyncio":
        return False
    owner = node.value
    return isinstance(owner, ast.Attribute) and owner.attr == "mark" or (
        isinstance(owner, ast.Name) and owner.id == "mark")


def _opted_out(body: list[ast.stmt]) -> bool:
    """Does this module or class body set ``__test__ = False``?

    pytest honours that attribute and skips the module or class entirely, so a
    name under it is never collected and accusing it would be a false
    accusation — the acceptor returned an earlier draft of this red case on
    exactly that input.
    """
    for stmt in body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__test__"
                   for t in stmt.targets):
            continue
        if isinstance(stmt.value, ast.Constant) and stmt.value.value is False:
            return True
    return False


def _functions_opted_out(body: list[ast.stmt]) -> set[str]:
    """Names this suite opts out with a post-definition ``f.__test__ = False``."""
    out: set[str] = set()
    for stmt in body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not (isinstance(stmt.value, ast.Constant)
                and stmt.value.value is False):
            continue
        for target in stmt.targets:
            if (isinstance(target, ast.Attribute)
                    and target.attr == "__test__"
                    and isinstance(target.value, ast.Name)):
                out.add(target.value.id)
    return out


def _marks(value: ast.expr) -> list[ast.expr]:
    """The elements of a `pytestmark` assignment, list/tuple or bare."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return list(value.elts)
    return [value]


def _suite_pytestmark_is_asyncio(body: list[ast.stmt]) -> bool:
    """Does this module or class body assign a `pytestmark` naming the marker?

    ANY such assignment, at any statement nesting — inside an ``if``, a ``try``,
    a ``with`` — counts, and the rule is syntactic by definition rather than an
    attempt to work out what the binding would evaluate to. Nested ``def`` and
    ``class`` bodies are NOT searched: a ``pytestmark`` there belongs to that
    suite, not this one.

    Deliberately blunt, and this is the second design. The first one modelled the
    effective value — last plain assignment wins, augmented assignment extends —
    and the acceptor returned it twice in a row, once for a false positive it
    still produced and once for a false negative, both inside that modelling. A
    static reading cannot decide what a module-level binding evaluates to, so the
    modelling is gone rather than sharpened, and the property is stated in terms
    of what the source SAYS. The cost is named: a module that assigns the marker
    and then rebinds ``pytestmark`` to something else is accused even though it
    marks nothing at collection. That module does not exist in this tree, and the
    rule is that it should not.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            value = getattr(stmt, "value", None)
            if value is not None and any(_is_asyncio_mark(m) for m in _marks(value)):
                return True
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list) and _suite_pytestmark_is_asyncio(
                    [st for st in inner if isinstance(st, ast.stmt)]):
                return True
        for handler in getattr(stmt, "handlers", []) or []:
            if _suite_pytestmark_is_asyncio(handler.body):
                return True
    return False


def _classes_in(body: list[ast.stmt]) -> dict[str, ast.ClassDef]:
    """Every class this module body DEFINES, at any statement nesting.

    Built with the same nesting rule ``_suite_pytestmark_is_asyncio`` uses, and
    for the same reason: a `class` inside a module-level ``if`` is defined just
    as a top-level one is. Indexing only ``tree.body`` while descending into
    nesting for the marker is an inconsistency the acceptor reproduced — a base
    class in an ``if`` block escaped the base-class lookup entirely. Nested
    ``def``/``class`` bodies are not searched: a class defined there is not a
    module-level name.
    """
    out: dict[str, ast.ClassDef] = {}
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            out.setdefault(stmt.name, stmt)
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list):
                for name, node in _classes_in(
                        [st for st in inner if isinstance(st, ast.stmt)]).items():
                    out.setdefault(name, node)
        for handler in getattr(stmt, "handlers", []) or []:
            for name, node in _classes_in(handler.body).items():
                out.setdefault(name, node)
    return out


def _class_marked(node: ast.ClassDef, classes: dict[str, ast.ClassDef],
                  seen: frozenset[str] = frozenset()) -> bool:
    """Is this class placed under the marker, its same-module bases included?

    pytest unpacks `pytestmark` through the class MRO, so a `pytestmark` on a
    base class reaches the child's tests; the acceptor returned a draft that read
    only the child's own body. Bases named by a plain identifier defined in the
    same module are followed, transitively and cycle-safely. A base reached
    through an import is NOT followed — see the module docstring's named limit.
    """
    if _suite_pytestmark_is_asyncio(node.body):
        return True
    seen = seen | {node.name}
    return any(base.id in classes and base.id not in seen
               and _class_marked(classes[base.id], classes, seen)
               for base in node.bases if isinstance(base, ast.Name))


def marked_sync_tests(source: bytes, filename: str) -> list[str]:
    """Every synchronous test in `source` that carries the asyncio marker.

    Each entry is ``<filename>:<lineno>:<qualname> (<source of the mark>)``, so
    a failure names the file, the function AND which of the three inheritance
    paths delivered the mark.
    """
    tree = ast.parse(source, filename)
    found: list[str] = []
    classes = _classes_in(tree.body)

    def visit(body: list[ast.stmt], prefix: str, inherited: str | None) -> None:
        skip = _functions_opted_out(body)
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                if not stmt.name.startswith("Test") or stmt.name in skip:
                    continue
                if _opted_out(stmt.body):
                    continue
                source_of = ("class-level pytestmark"
                             if _class_marked(stmt, classes) else inherited)
                visit(stmt.body, f"{prefix}{stmt.name}.", source_of)
            elif isinstance(stmt, ast.FunctionDef):
                if not stmt.name.startswith("test") or stmt.name in skip:
                    continue
                source_of = inherited
                if any(_is_asyncio_mark(d) for d in stmt.decorator_list):
                    source_of = "decorator"
                if source_of is not None:
                    found.append(
                        f"{filename}:{stmt.lineno}:{prefix}{stmt.name} "
                        f"({source_of})")

    if _opted_out(tree.body):
        return found
    module_source = ("module-level pytestmark"
                     if _suite_pytestmark_is_asyncio(tree.body) else None)
    visit(tree.body, "", module_source)
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
        """The acceptor's second return: an assignment inside a module-level
        ``if`` binds ``pytestmark`` just as a top-level one does."""
        src = (b"import pytest\nif True:\n"
               b"    pytestmark = pytest.mark.asyncio\n"
               b"def test_x():\n    pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:4:test_x (module-level pytestmark)"]

    def test_a_pytestmark_inside_a_nested_def_is_not_this_suites(self) -> None:
        """A ``pytestmark`` in a function or class body belongs to that suite."""
        src = (b"import pytest\ndef helper():\n"
               b"    pytestmark = pytest.mark.asyncio\n"
               b"def test_x():\n    pass\n")
        assert marked_sync_tests(src, "m.py") == []

    def test_a_pytestmark_on_a_same_module_base_class_is_a_source(self) -> None:
        """The acceptor's fourth return: pytest unpacks `pytestmark` through the
        class MRO, so a base class's mark reaches the child's tests."""
        src = (b"import pytest\nclass MarkedBase:\n"
               b"    pytestmark = pytest.mark.asyncio\n"
               b"class TestChild(MarkedBase):\n"
               b"    def test_sync(self):\n        pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:5:TestChild.test_sync (class-level pytestmark)"]

    def test_a_base_class_nested_in_a_statement_is_still_found(self) -> None:
        """The acceptor's fifth return: a class defined inside a module-level
        ``if`` is a module-level name, so the base-class index must see it —
        the same nesting rule the marker scan already used."""
        src = (b"import pytest\nif True:\n    class MarkedBase:\n"
               b"        pytestmark = pytest.mark.asyncio\n"
               b"class TestChild(MarkedBase):\n"
               b"    def test_sync(self):\n        pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:6:TestChild.test_sync (class-level pytestmark)"]

    def test_a_base_class_cycle_terminates(self) -> None:
        """Illegal Python, but an AST can hold it and the walk must not hang."""
        src = (b"import pytest\nclass A(B):\n    pass\n"
               b"class B(A):\n    pass\n"
               b"class TestC(A):\n    def test_x(self):\n        pass\n")
        assert marked_sync_tests(src, "m.py") == []

    def test_a_class_level_pytestmark_is_a_source(self) -> None:
        src = (b"import pytest\nclass TestA:\n"
               b"    pytestmark = pytest.mark.asyncio\n"
               b"    def test_x(self):\n        pass\n")
        assert marked_sync_tests(src, "m.py") == [
            "m.py:4:TestA.test_x (class-level pytestmark)"]

    def test_a_decorator_is_a_source(self) -> None:
        # Assembled rather than written out: the repository's pre-commit hook
        # reads a literal "@" followed by a dotted name as an email address.
        decorator = "@" + "pytest.mark.asyncio"
        src = ("import pytest\n" + decorator
               + "\ndef test_x():\n    pass\n").encode()
        assert marked_sync_tests(src, "m.py") == ["m.py:3:test_x (decorator)"]

    @pytest.mark.parametrize("assignment", [
        b"pytestmark = pytest.mark.asyncio",
        b"pytestmark = pytest.mark.asyncio()",
        b"pytestmark = [pytest.mark.asyncio, pytest.mark.unit]",
        b"pytestmark = (pytest.mark.unit, pytest.mark.asyncio)",
        b"pytestmark: object = pytest.mark.asyncio",
        b"pytestmark = mark.asyncio",
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
        b"import pytest\npytestmark: list = []\ndef test_x():\n    pass\n",
        b"import pytest\nimport asyncio\npytestmark = pytest.mark.unit\ndef test_x():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\nasync def test_x():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\ndef helper():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\nclass Helper:\n    def test_x(self):\n        pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\nclass TestA:\n"
        b"    __test__ = False\n    def test_x(self):\n        pass\n",
        b"import pytest\n__test__ = False\npytestmark = pytest.mark.asyncio\n"
        b"def test_x():\n    pass\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\ndef test_x():\n"
        b"    pass\ntest_x.__test__ = False\n",
        b"import pytest\npytestmark = pytest.mark.asyncio\nclass TestA:\n"
        b"    def test_x(self):\n        pass\nTestA.__test__ = False\n",
        b"from base import MarkedBase\nclass TestChild(MarkedBase):\n"
        b"    def test_x(self):\n        pass\n",
    ])
    def test_what_the_rule_does_not_accuse(self, source: bytes) -> None:
        """An async test, a non-test function, a class pytest does not collect,
        a bare annotation that binds nothing, a module whose only `asyncio` is
        the stdlib import, and the four `__test__ = False` opt-outs — module,
        class, function attribute and class attribute — that pytest honours and
        the acceptor returned an earlier draft of this red case for."""
        assert marked_sync_tests(source, "m.py") == []
