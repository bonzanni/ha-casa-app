"""§14 test 9a (INV-MEM-035): the wipe loop runs only in the container-start
step. Nothing but ``memory.maintenance.run`` calls it, and nothing calls
``run`` but the step's own entry point, so ``casa-main`` (which imports the
module for the intent check and the doors) never runs it."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

CODE = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"

#: The loops and who may call them.
LOOPS = {"wipe_loop": {("memory/maintenance.py", "run")},
         "run": {("memory/maintenance.py", "main")}}


def callers(code: Path) -> dict[str, set[tuple[str, str]]]:
    """Every function that calls (or hands on) a maintenance loop by name."""
    found: dict[str, set[tuple[str, str]]] = {n: set() for n in LOOPS}
    for path in sorted(code.rglob("*.py")):
        rel = path.relative_to(code).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_maintenance = rel == "memory/maintenance.py" or any(
            isinstance(n, (ast.Import, ast.ImportFrom)) and "maintenance" in
            ast.unparse(n) for n in ast.walk(tree))
        if not imports_maintenance:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                name = None
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name) and rel == "memory/maintenance.py":
                    name = node.id
                # ``run`` is the step's own name in its module, or an
                # attribute of the maintenance module elsewhere (never, say,
                # ``asyncio.run``).
                is_step = name == "run" and (
                    isinstance(node, ast.Name)
                    or "maintenance" in ast.unparse(node.value))
                if name == "wipe_loop" or is_step:
                    found[name].add((rel, fn.name))
    return found


def test_only_the_step_runs_the_wipe_loop():
    assert callers(CODE) == LOOPS


def test_the_scan_reports_a_casa_main_caller(tmp_path):
    source = (CODE / "memory" / "maintenance.py").read_text(
        encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "maintenance.py").write_text(source)
    (tmp_path / "casa_core.py").write_text(
        "from memory import maintenance as memory_maintenance\n"
        "async def main():\n"
        "    memory_maintenance.wipe_loop(root='/')\n"
        "    memory_maintenance.run()\n")
    found = callers(tmp_path)
    assert ("casa_core.py", "main") in found["wipe_loop"]
    assert ("casa_core.py", "main") in found["run"]


def test_the_step_script_runs_the_module_entry_point():
    script = (CODE.parents[1] / "etc/s6-overlay/scripts/memory-maintenance.sh"
              ).read_text(encoding="utf-8")
    lines = [ln for ln in script.splitlines() if "python3" in ln]
    assert len(lines) == 1
    assert "PYTHONPATH=/opt/casa /opt/casa/venv/bin/python3 -m memory.maintenance" in lines[0]
    assert script.rstrip().endswith("exit 0")
