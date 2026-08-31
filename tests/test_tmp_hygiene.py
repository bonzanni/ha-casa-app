"""#778: every temp artifact a test creates must land in a managed directory.

The defect this closes was not five bad lines, it was a bad *method*. The issue
enumerated the population with `grep mkdtemp`, which missed the largest site
(`tempfile.mkstemp(...)[1]` in `tests/test_boot_replay.py`, which leaked a file
AND its open descriptor on each of 51 call sites) and would also have missed the
one call written with a non-literal receiver
(`pytest.importorskip("tempfile").mkdtemp()`). So the guard is a rule over the
whole population rather than an assertion about the six calls that happened to
be leaking: **every `mkdtemp`/`mkstemp` call under `tests/` names an explicit
`dir=`**, and the directory a test has in hand is a pytest-managed one
(`tmp_path`), which pytest reclaims.

Deliberately scoped, and the scope is a limit rather than coverage claimed:

- `dir=` is a property OF THE CALL, so it is decidable by reading the call. A
  rule about "is it cleaned up later" is not: the cleanup can be arbitrarily far
  away, or in a fixture, or absent, and an AST cannot tell those apart.
- The predicate is RECEIVER-AGNOSTIC — any call whose target is named `mkdtemp`
  or `mkstemp`, however it was reached. A `tempfile.`-only predicate reports 5
  where the true count is 6, which is the same undercount in a new costume.
- It does NOT police `TemporaryDirectory` / `NamedTemporaryFile`. Those sites in
  this tree are already correct — `TemporaryDirectory` used as a context manager
  self-cleans (`tests/test_unix_socket_runner.py`,
  `tests/test_finalize_engagement.py`), and
  `tests/test_workflow_shell.py`'s `NamedTemporaryFile(delete=False)` unlinks in
  a `finally` — and a rule that had to allowlist correct code would rot.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_FACTORIES = ("mkdtemp", "mkstemp")


def _called_name(node: ast.Call) -> str | None:
    """The name being called, whatever the receiver expression is.

    `tempfile.mkdtemp()`, `tf.mkdtemp()` and
    `pytest.importorskip("tempfile").mkdtemp()` all answer `mkdtemp`; a bare
    `mkdtemp()` from `from tempfile import mkdtemp` answers it too.
    """
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def test_mkdtemp_and_mkstemp_calls_name_a_managed_dir():
    """Counts, not statuses: the number of calls lacking `dir=` must be 0.

    At the commit that introduced this guard the count was 6 — three in
    `tests/test_boot_replay.py`, one each in `tests/test_mock_sdk_tool_invoke.py`,
    `tests/test_config_trigger_tools.py` and
    `tests/test_config_sync_reminder_race.py` — and one targeted run of
    `tests/test_boot_replay.py` alone left 66 directories and 52 files in the
    system temp directory, with nothing anywhere that reclaims them.
    """
    tests_dir = Path(__file__).resolve().parent
    violations: list[str] = []
    scanned = 0
    for path in sorted(tests_dir.glob("*.py")):
        tree = ast.parse(path.read_bytes(), str(path))
        scanned += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name not in _FACTORIES:
                continue
            if any(kw.arg == "dir" for kw in node.keywords):
                continue
            violations.append(f"{path.name}:{node.lineno}:{name}")

    assert scanned > 100, (
        f"only {scanned} test modules were parsed — the glob found almost "
        f"nothing, so a count of 0 violations would prove nothing")
    assert len(violations) == 0, (
        "tempfile calls without explicit dir=:\n" + "\n".join(violations))
