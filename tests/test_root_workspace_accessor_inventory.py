"""CI inventory guard — containment stage 2, Task 4.

Every file below is root-only engagement run-state and MUST live in the
root-only control dir (``/data/engagement-ctl/<id>/``, ``drivers.workspace.
control_dir``), never joined to the uid-owned engagements WORKSPACE root
(``/data/engagements/<id>/``): a workspace path is reachable through
``--add-dir`` by the engagement's own (eventually unprivileged, Stage 2/6/8)
CLI process, so a control-state file placed there is a symlink-primitive
target again — exactly the hole this whole containment stage exists to close.

This test parses (AST, not grep — grep can't tell a real path-join from a
docstring mentioning the same filename) the four root modules that read or
write engagement run-state and fails if any of the control-only basenames is
ever joined — via ``os.path.join``, the pathlib ``/`` operator, or an
f-string — to a symbol that denotes the engagements WORKSPACE root or a
per-engagement workspace directory in this codebase's own naming convention.

The symbol list is intentionally a fixed, conservative allowlist (per the
Task 4 brief) rather than a full dataflow analysis: it does not prove a given
variable was DERIVED from the workspace root, only that it is one of the
names this codebase already uses to mean "the per-engagement workspace
directory" in these four files. That is enough to catch the shape of the
violation that existed here before the Task 4 relocation (e.g. ``ws = Path
(engagements_root) / engagement_id`` followed by ``ws / "stdin.fifo"``) and
to prevent a new one from reappearing, without pretending to be a general
points-to analysis.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.unit]

_ROOT = pathlib.Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa"

# Files where root (casa-core) reads/writes engagement run-state.
_ROOT_MODULES = [
    "drivers/workspace.py",
    "drivers/claude_code_driver.py",
    "casa_core.py",
    "drivers/topic_stream.py",
]

# Run-state basenames that belong EXCLUSIVELY under the control dir. Any of
# these joined to the workspace root anywhere in a root module is a live
# containment hole.
_CONTROL_ONLY = frozenset({
    ".session_id",
    ".spawn_epoch",
    ".casa-meta.json",
    ".inbound_spool.jsonl",
    ".stream_cursor.json",
    ".executor_memory",
    "stdin.fifo",
})

# Symbol names this codebase's own root modules use to mean "the engagements
# WORKSPACE root" or "a specific per-engagement workspace directory" — as
# opposed to ``drivers.workspace.control_dir`` / ``CONTROL_ROOT`` and the
# per-file path helpers, which are the only sanctioned way to reach a
# _CONTROL_ONLY file. Attribute names are matched on the trailing attr only
# (covers ``self._engagements_root``, ``self._something.ws_dir``, etc.).
_WORKSPACE_ROOT_NAMES = frozenset({
    "engagements_root",
    "_ENGAGEMENTS_ROOT",
    "_engagements_root",
    "ws", "ws_dir", "_ws_dir",
    "workspace_path", "workspace_dir",
})

# A literal reference to the workspace root's own path segment/prefix also
# counts — e.g. a hand-rolled ``f"/data/engagements/{id}/.session_id"`` that
# never goes through a Name at all.
_WORKSPACE_ROOT_LITERAL_MARKERS = ("engagements",)


def _is_workspace_root_ref(node: ast.AST) -> bool:
    """True if ``node`` (or anything nested inside it) denotes the
    engagements workspace root/dir per this file's naming convention."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _WORKSPACE_ROOT_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _WORKSPACE_ROOT_NAMES:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(m in sub.value for m in _WORKSPACE_ROOT_LITERAL_MARKERS):
                return True
    return False


def _control_literal_value(node: ast.AST) -> str | None:
    """Return the _CONTROL_ONLY member ``node`` denotes, resolving through a
    module-level ``NAME = "literal"`` alias (e.g. claude_code_driver.py's
    ``_SPOOL_FILENAME``); else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in _CONTROL_ONLY else None
    return None


def _flatten_div_chain(node: ast.AST) -> list[ast.AST]:
    """Flatten a pathlib ``a / b / c`` chain (nested ``BinOp(Div)``) into its
    leaf operands, so a 3+-segment join is inspected as one set of leaves."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _flatten_div_chain(node.left) + _flatten_div_chain(node.right)
    return [node]


def _collect_const_aliases(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so a join built from the
    ALIAS (not the literal itself) is still resolved — e.g.
    ``ws / _SPOOL_FILENAME`` where ``_SPOOL_FILENAME = ".inbound_spool.jsonl"``.
    """
    aliases: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            aliases[node.targets[0].id] = node.value.value
    return aliases


# Fix-loop round 1 (Important 2): a narrow, explicit escape hatch for a
# LEGACY READ — code that reads (never writes) a control-only file at the
# workspace path, solely to migrate a pre-Task-4 engagement's data forward
# into the control dir. This is a real, deliberate exception (see
# drivers/workspace.py::load_casa_meta), not a loophole: it is line-scoped
# (the pragma must sit on the exact offending line, so it can't blanket-
# exempt a whole file or function), and it is reviewed the same as any other
# line — a reviewer sees the pragma AND the join together in the diff.
_LEGACY_READ_PRAGMA = "containment-legacy-read-only:"


def _scan_for_workspace_joins(src: str, control_only: frozenset) -> list[str]:
    """Return one description string per offending join found in ``src``."""
    tree = ast.parse(src)
    const_aliases = _collect_const_aliases(tree)
    src_lines = src.splitlines()

    def _control_value(node: ast.AST) -> str | None:
        direct = _control_literal_value(node)
        if direct is not None:
            return direct
        if isinstance(node, ast.Name) and node.id in const_aliases:
            val = const_aliases[node.id]
            return val if val in control_only else None
        return None

    def _exempt(lineno: int) -> bool:
        if not (1 <= lineno <= len(src_lines)):
            return False
        return _LEGACY_READ_PRAGMA in src_lines[lineno - 1]

    offenders: list[str] = []

    for node in ast.walk(tree):
        # Case 1: os.path.join(..., <workspace-root-ish>, ..., <control literal>, ...)
        # (also catches ``path.join(...)`` / any ``*.join(...)`` call — the
        # attribute name is the discriminator, not the base object, so this
        # stays robust to `import os.path as path`-style aliasing.)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
        ):
            args = node.args
            has_root = any(_is_workspace_root_ref(a) for a in args)
            has_control = any(_control_value(a) is not None for a in args)
            if has_root and has_control and not _exempt(node.lineno):
                offenders.append(
                    f"line {node.lineno}: os.path.join(...) mixes the "
                    "workspace root with a control-only basename")

        # Case 2: pathlib ``a / b / ...`` chains.
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            leaves = _flatten_div_chain(node)
            has_root = any(_is_workspace_root_ref(leaf) for leaf in leaves)
            has_control = any(_control_value(leaf) is not None for leaf in leaves)
            if has_root and has_control and not _exempt(node.lineno):
                offenders.append(
                    f"line {node.lineno}: pathlib '/' chain mixes the "
                    "workspace root with a control-only basename")

        # Case 3: f-strings splicing a workspace-root value next to a
        # control-only literal chunk.
        elif isinstance(node, ast.JoinedStr):
            has_root = any(
                (isinstance(v, ast.FormattedValue) and _is_workspace_root_ref(v.value))
                or (isinstance(v, ast.Constant) and isinstance(v.value, str)
                    and any(m in v.value for m in _WORKSPACE_ROOT_LITERAL_MARKERS))
                for v in node.values
            )
            has_control = any(
                isinstance(v, ast.Constant) and isinstance(v.value, str)
                and any(name in v.value for name in control_only)
                for v in node.values
            )
            if has_root and has_control and not _exempt(node.lineno):
                offenders.append(
                    f"line {node.lineno}: f-string mixes the workspace "
                    "root with a control-only basename")

    # A single Div chain is visited once per nested BinOp node by ast.walk —
    # dedupe by line so one real offender isn't reported N times for an
    # N-segment chain.
    seen: set[str] = set()
    deduped = []
    for o in offenders:
        key = o.split(":", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    return deduped


def test_no_control_state_under_workspace_root():
    offenders: list[str] = []
    for m in _ROOT_MODULES:
        src = (_ROOT / m).read_text(encoding="utf-8")
        found = _scan_for_workspace_joins(src, _CONTROL_ONLY)
        offenders.extend(f"{m}: {f}" for f in found)
    assert offenders == [], (
        "control-state basename joined to the workspace root in a root "
        f"module (must live under drivers.workspace.control_dir instead): {offenders}"
    )


class TestScannerCatchesThePreRelocationShape:
    """Pin the scanner ITSELF against the exact patterns Task 4 removed —
    protects the guard from silently degrading into a no-op alongside a
    future refactor of the scanner internals."""

    def test_catches_binop_chain_through_a_ws_alias(self):
        src = (
            "import os\n"
            "from pathlib import Path\n"
            "class D:\n"
            "    def f(self, engagement_id):\n"
            "        ws = Path(self._engagements_root) / engagement_id\n"
            "        fifo = ws / \"stdin.fifo\"\n"
            "        return fifo\n"
        )
        assert _scan_for_workspace_joins(src, _CONTROL_ONLY) != []

    def test_catches_os_path_join_direct(self):
        src = (
            "import os\n"
            "def f(engagements_root, rec_id):\n"
            "    return os.path.join(engagements_root, rec_id, \"stdin.fifo\")\n"
        )
        assert _scan_for_workspace_joins(src, _CONTROL_ONLY) != []

    def test_catches_const_alias_join(self):
        src = (
            "from pathlib import Path\n"
            "_SPOOL_FILENAME = \".inbound_spool.jsonl\"\n"
            "def f(engagements_root, eid):\n"
            "    ws = Path(engagements_root) / eid\n"
            "    return ws / _SPOOL_FILENAME\n"
        )
        assert _scan_for_workspace_joins(src, _CONTROL_ONLY) != []

    def test_catches_fstring_join(self):
        src = (
            "def f(engagement_id):\n"
            "    return f\"/data/engagements/{engagement_id}/.session_id\"\n"
        )
        assert _scan_for_workspace_joins(src, _CONTROL_ONLY) != []

    def test_clean_control_dir_helper_is_not_flagged(self):
        src = (
            "import os\n"
            "CONTROL_ROOT = \"/data/engagement-ctl\"\n"
            "def control_dir(engagement_id):\n"
            "    return os.path.join(CONTROL_ROOT, engagement_id)\n"
            "def session_id_path(engagement_id):\n"
            "    return os.path.join(control_dir(engagement_id), \".session_id\")\n"
        )
        assert _scan_for_workspace_joins(src, _CONTROL_ONLY) == []

    def test_workspace_join_of_a_non_control_name_is_not_flagged(self):
        """Sanity: the scanner must not be so broad it flags every join
        touching the workspace root — only the seven control-only names."""
        src = (
            "import os\n"
            "def f(engagements_root, eid):\n"
            "    return os.path.join(engagements_root, eid, \"CLAUDE.md\")\n"
        )
        assert _scan_for_workspace_joins(src, _CONTROL_ONLY) == []

    def test_legacy_read_pragma_exempts_only_its_own_line(self):
        """Fix-loop round 1 (Important 2): the pragma is LINE-scoped — an
        unmarked join two lines away in the same function must still be
        caught, so the escape hatch can't be widened into a blanket
        exemption by accident."""
        src = (
            "from pathlib import Path\n"
            "def f(workspace_path, ws_dir):\n"
            "    legacy = Path(workspace_path) / \".casa-meta.json\"  "
            f"# {_LEGACY_READ_PRAGMA} pre-Task-4 fallback\n"
            "    other = Path(ws_dir) / \".casa-meta.json\"\n"
            "    return legacy, other\n"
        )
        found = _scan_for_workspace_joins(src, _CONTROL_ONLY)
        assert len(found) == 1 and "line 4" in found[0], found
