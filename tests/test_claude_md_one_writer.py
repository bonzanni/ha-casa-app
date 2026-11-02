"""A workspace's root ``CLAUDE.md`` is written only by
``drivers.workspace.write_claude_md``, which renders it through
``memory.executor_block.render``: the memory wipe blanks the one marker pair
in that file and relies on no other writer putting markers there
(INV-MEM-038).

A static scan of ``drivers/`` and ``tools.py``: every call that writes,
copies, moves or links a file, and names ``CLAUDE.md`` among its arguments or
its receiver — directly, or through a name assigned from an expression that
does — is reported by enclosing function. Only the one writer may appear."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

CODE = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
WRITER = {("drivers/workspace.py", "write_claude_md")}

_WRITE_CALLS = {
    "write_text", "write_bytes", "atomic_write_beneath", "atomic_write_text",
    "atomic_write_json", "copy", "copy2", "copyfile", "copytree", "move",
    "replace", "rename", "symlink", "link", "symlink_to", "hardlink_to",
}


def _mentions(node: ast.AST, tainted: set[str]) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
                n.value == "CLAUDE.md" or n.value.endswith("/CLAUDE.md")):
            return True
        if isinstance(n, ast.Name) and n.id in tainted:
            return True
    return False


def _is_write(call: ast.Call) -> bool:
    f = call.func
    name = getattr(f, "attr", None) or getattr(f, "id", None)
    if name in _WRITE_CALLS:
        return True
    if name == "open" and len(call.args) > 1:
        mode = call.args[1]
        return isinstance(mode, ast.Constant) and any(
            c in str(mode.value) for c in "wax+")
    return False


def writers(code: Path) -> set[tuple[str, str]]:
    found = set()
    files = sorted((code / "drivers").glob("*.py")) + [code / "tools.py"]
    for path in files:
        rel = path.relative_to(code).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            tainted: set[str] = set()
            for _ in range(3):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Assign) and _mentions(n.value, tainted):
                        tainted |= {t.id for t in n.targets
                                    if isinstance(t, ast.Name)}
            for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                if not _is_write(call):
                    continue
                parts = [*call.args, *(k.value for k in call.keywords)]
                if isinstance(call.func, ast.Attribute):
                    parts.append(call.func.value)
                if any(_mentions(p, tainted) for p in parts):
                    found.add((rel, fn.name))
    return found


def test_only_the_one_writer_writes_a_workspace_claude_md():
    assert writers(CODE) == WRITER


def test_the_scan_reports_another_writer(tmp_path):
    (tmp_path / "drivers").mkdir()
    (tmp_path / "drivers" / "x.py").write_text(
        "import shutil\n"
        "def write_claude_md(ws, text):\n"
        "    atomic_write_beneath(ws, 'CLAUDE.md', text)\n"
        "def copy_plain(src, ws):\n"
        "    target = ws / 'CLAUDE.md'\n"
        "    shutil.copy2(src, target)\n"
        "def append(ws):\n"
        "    with open(ws + '/CLAUDE.md', 'a') as fh:\n"
        "        fh.write('x')\n")
    (tmp_path / "tools.py").write_text(
        "def note(ws):\n"
        "    (ws / 'CLAUDE.md').write_text('x')\n")
    assert writers(tmp_path) == {
        ("drivers/x.py", "write_claude_md"), ("drivers/x.py", "copy_plain"),
        ("drivers/x.py", "append"), ("tools.py", "note")}
