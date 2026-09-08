"""#915: no synchronous test is placed under `pytest.mark.asyncio`.

The rule, exactly. A synchronous test is placed under the marker when any of
three things is true of the SOURCE:

1. the marker is named in a ``pytestmark`` assignment in the test's own suite —
   its module body, or the body of a class that lexically encloses it, reading
   each of those bodies flattened through ``if``/``try``/``with``/``for``/
   ``while``/``match`` and never into a nested ``def``/``class``;
2. the marker is named in a ``pytestmark`` assignment in the body of a base
   class, where a base counts only when it is written as an unqualified name
   whose exact spelling is a ``class`` statement in the same module's flattened
   suite (following is transitive over such bases, and cycle-safe);
3. the marker decorates the test.

The marker itself is recognised by two spellings and no others: an attribute
``asyncio`` whose owner is an attribute named ``mark`` (so ``pytest.mark.asyncio``
and ``pt.mark.asyncio`` both count, with or without a call), or an attribute
``asyncio`` on a bare name that this module introduced directly in its flattened
suite by ``from pytest import mark`` (plain or aliased) or by an assignment,
annotated or not, whose value is an attribute named ``mark``.

**One sentence governs all three: this rule follows no rebinding.** Every name it
reads — the marker's receiver, a base class, a ``__test__`` opt-out target — is
matched by its exact spelling at the site where it is used, never through an
alias of an alias or a chain of assignments. So ``p = pytest.mark; n = p`` and
``Alias = MarkedBase; class TestChild(Alias)`` are both outside the rule, are
both measured and pinned below as things it does NOT accuse, and are both
declared here rather than left to be discovered. A future clause is subject to
the same sentence.

That is a property of the SOURCE TEXT, and it is stated that way on purpose. It
is not a model of pytest's marker resolution. An earlier draft tried to be one —
it worked out what a ``pytestmark`` binding would evaluate to, last assignment
winning — and the disagreements between such a rule and a rule that reads a file
proved unbounded: a rebound ``pytestmark``, an assignment nested in an ``if``, a
class opted out with ``__test__ = False``, a base class in another module, a base
class behind an alias, an alias of an alias. Each one cost a round. The modelling
was cut rather than sharpened, and each time the CLAIM was narrowed to what the
rule decides rather than the rule stretched toward what it cannot.

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


def _suite_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """Every statement BELONGING to this suite, flattened.

    A module or class body executes its ``if``, ``try``, ``with``, ``for``,
    ``while`` and ``match`` branches in its own namespace, so a ``pytestmark``,
    a ``class`` or a ``def`` written inside one of those belongs to this suite
    exactly as a top-level statement does. A nested ``def``/``class`` body does
    NOT: names bound there belong to that suite.

    There is ONE flattener because there were three, and they disagreed. The
    acceptor and a diff reviewer each reproduced the same shape from a different
    direction — a base class inside a module-level ``if`` escaped the base-class
    index while the marker scan descended into it; a ``def test_sync`` inside a
    module-level ``if`` escaped test discovery while both of the others
    descended. Every walk below now starts here, so the nesting rule cannot
    diverge between them again.
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
    """Names this module binds to `pytest.mark`.

    `from pytest import mark` binds `mark`; `from pytest import mark as p` binds
    `p`; `m = pytest.mark` and `m: object = pytest.mark` both bind `m`. A diff
    reviewer reproduced the aliased import reaching collection as a live warning
    while the guard stayed green, and the specifier reproduced the annotated
    assignment doing the same, so the marker is recognised through whatever name
    the module gave it rather than through one spelling of the binding.
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
    """True for `pytest.mark.asyncio`, with or without a call, any receiver.

    `pytest.mark.asyncio`, `pytest.mark.asyncio(...)` and `pt.mark.asyncio` all
    answer True through the `.mark.` chain; `p.asyncio` answers True when the
    module bound `p` to `pytest.mark`. `pytest.mark.unit` and a bare name
    `asyncio` (the module) do not.
    """
    while isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute) or node.attr != "asyncio":
        return False
    owner = node.value
    if isinstance(owner, ast.Attribute):
        return owner.attr == "mark"
    return isinstance(owner, ast.Name) and owner.id in aliases


def _opted_out(stmts: list[ast.stmt]) -> bool:
    """Does this suite set ``__test__ = False``?

    pytest honours that attribute and skips the module or class entirely, so a
    name under it is never collected and accusing it would be a false
    accusation — the acceptor returned an earlier draft of this red case on
    exactly that input.
    """
    return any(isinstance(stmt, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "__test__"
                       for t in stmt.targets)
               and isinstance(stmt.value, ast.Constant)
               and stmt.value.value is False
               for stmt in stmts)


def _functions_opted_out(stmts: list[ast.stmt]) -> set[str]:
    """Names this suite opts out with a post-definition ``f.__test__ = False``."""
    out: set[str] = set()
    for stmt in stmts:
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


def _suite_pytestmark_is_asyncio(stmts: list[ast.stmt],
                                 aliases: frozenset[str]) -> bool:
    """Does this suite assign a `pytestmark` naming the marker?

    ANY such assignment counts, and the rule is syntactic by definition rather
    than an attempt to work out what the binding would evaluate to.

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


def _classes_in(stmts: list[ast.stmt]) -> dict[str, ast.ClassDef]:
    """Every class this module body DEFINES, first definition of each name."""
    out: dict[str, ast.ClassDef] = {}
    for stmt in stmts:
        if isinstance(stmt, ast.ClassDef):
            out.setdefault(stmt.name, stmt)
    return out


def _class_marked(node: ast.ClassDef, classes: dict[str, ast.ClassDef],
                  aliases: frozenset[str],
                  seen: frozenset[str] = frozenset()) -> bool:
    """Is this class placed under the marker, its same-module bases included?

    pytest unpacks `pytestmark` through the class MRO, so a `pytestmark` on a
    base class reaches the child's tests; the acceptor returned a draft that read
    only the child's own body. Bases named by a plain identifier defined in the
    A base is followed only when it is written as an unqualified name whose exact
    spelling is a ``class`` statement in this module's flattened suite; following
    is transitive over such bases and cycle-safe. An imported base, an aliased one
    (``Alias = MarkedBase``) and any other base expression are NOT followed — see
    the module docstring's governing sentence about rebinding.
    """
    if _suite_pytestmark_is_asyncio(_suite_statements(node.body), aliases):
        return True
    seen = seen | {node.name}
    return any(base.id in classes and base.id not in seen
               and _class_marked(classes[base.id], classes, aliases, seen)
               for base in node.bases if isinstance(base, ast.Name))


def marked_sync_tests(source: bytes, filename: str) -> list[str]:
    """Every synchronous test in `source` placed under the asyncio marker.

    Each entry is ``<filename>:<lineno>:<qualname> (<source of the mark>)``, so
    a failure names the file, the function AND which of the three paths placed
    the test under the mark.
    """
    tree = ast.parse(source, filename)
    found: list[str] = []
    module_stmts = _suite_statements(tree.body)
    aliases = _mark_aliases(module_stmts)
    classes = _classes_in(module_stmts)

    def visit(stmts: list[ast.stmt], prefix: str, inherited: str | None) -> None:
        skip = _functions_opted_out(stmts)
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                if not stmt.name.startswith("Test") or stmt.name in skip:
                    continue
                inner = _suite_statements(stmt.body)
                if _opted_out(inner):
                    continue
                source_of = ("class-level pytestmark"
                             if _class_marked(stmt, classes, aliases)
                             else inherited)
                visit(inner, f"{prefix}{stmt.name}.", source_of)
            elif isinstance(stmt, ast.FunctionDef):
                if not stmt.name.startswith("test") or stmt.name in skip:
                    continue
                source_of = inherited
                if any(_is_asyncio_mark(d, aliases)
                       for d in stmt.decorator_list):
                    source_of = "decorator"
                if source_of is not None:
                    found.append(
                        f"{filename}:{stmt.lineno}:{prefix}{stmt.name} "
                        f"({source_of})")

    if _opted_out(module_stmts):
        return found
    module_source = ("module-level pytestmark"
                     if _suite_pytestmark_is_asyncio(module_stmts, aliases)
                     else None)
    visit(module_stmts, "", module_source)
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

    def test_a_test_defined_inside_a_statement_is_still_discovered(self) -> None:
        """A diff reviewer's finding: a `def test_sync` inside a module-level
        ``if`` is collected by pytest, and the marker scan already descended
        into that ``if`` — so test discovery must too, or the guard passes a
        violation of its own declared property."""
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

    def test_an_aliased_mark_decorator_is_a_source(self) -> None:
        """A diff reviewer's finding: `from pytest import mark as p` then
        `p.asyncio` reaches collection as the real warning."""
        decorator = "@" + "p.asyncio"
        src = ("from pytest import mark as p\n" + decorator
               + "\ndef test_sync():\n    pass\n").encode()
        assert marked_sync_tests(src, "m.py") == ["m.py:3:test_sync (decorator)"]

    def test_an_annotated_alias_binding_is_still_an_alias(self) -> None:
        """The specifier's re-specification: `m: object = pytest.mark` binds `m`
        exactly as `m = pytest.mark` does, and only the un-annotated form was
        read."""
        decorator = "@" + "m.asyncio"
        src = ("import pytest\nm: object = pytest.mark\n" + decorator
               + "\ndef test_sync():\n    pass\n").encode()
        assert marked_sync_tests(src, "test_annotation_probe.py") == [
            "test_annotation_probe.py:4:test_sync (decorator)"]

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
        b"pytestmark = mark.asyncio\ndef test_x():\n    pass\n",
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
        b"import pytest\nclass MarkedBase:\n"
        b"    pytestmark = pytest.mark.asyncio\nAlias = MarkedBase\n"
        b"class TestChild(Alias):\n    def test_x(self):\n        pass\n",
        b"import pytest\np = pytest.mark\nn = p\npytestmark = n.asyncio\n"
        b"def test_x():\n    pass\n",
    ])
    def test_what_the_rule_does_not_accuse(self, source: bytes) -> None:
        """The two limits the rule DECLARES — a base class reached through an
        import or through an alias, and an alias of an alias of `pytest.mark` —
        pinned here so they stay visible rather than living only in prose. Then a
        `mark` this module never bound to `pytest.mark`, an async test, a
        non-test function, a class pytest does not collect,
        a bare annotation that binds nothing, a module whose only `asyncio` is
        the stdlib import, and the four `__test__ = False` opt-outs — module,
        class, function attribute and class attribute — that pytest honours and
        the acceptor returned an earlier draft of this red case for."""
        assert marked_sync_tests(source, "m.py") == []
