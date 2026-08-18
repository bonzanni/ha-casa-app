"""Every third-party import the app makes must be DECLARED in the manifest.

The shipped image installs exactly ``casa/requirements.txt``. An import that
works anyway — because some declared dependency happens to drag it in — is a
time bomb: the day ``mcp`` or ``claude-agent-sdk`` drops that transitive
dependency, every fresh install breaks at boot, and nothing in this repo said
the app needed it. This is not hypothetical: ``anyio`` and ``pydantic`` were
imported directly for months while only transitive pins guaranteed them.

Static analysis on purpose: the test parses the tree rather than importing it,
so a conditional or lazily-imported module is still counted, and the test
cannot be fooled by whatever happens to be importable in the running venv.
"""

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"
MANIFEST = REPO / "casa" / "requirements.txt"


def _canonical(name: str) -> str:
    """PEP 503 name canonicalization: case and [-_.] runs are equivalent."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_distributions() -> set[str]:
    """The distribution names the manifest declares, canonicalized."""
    declared = set()
    for line in MANIFEST.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        assert m, f"unparseable requirement line: {line!r}"
        declared.add(_canonical(m.group(0)))
    return declared


_PY_DIR_CACHE: dict[Path, bool] = {}


def _bears_python(p: Path) -> bool:
    """True when a directory contains any Python at all, however nested.

    A bare directory name is NOT a module: classifying every immediate
    directory as local let an empty data directory named like a dependency
    silently exempt the real, undeclared import (Sol, v0.222.0 review) —
    the exact silent pass this test exists to prevent.
    """
    if p not in _PY_DIR_CACHE:
        _PY_DIR_CACHE[p] = next(p.rglob("*.py"), None) is not None
    return _PY_DIR_CACHE[p]


def _local_names(directory: Path) -> set[str]:
    """The module names importable as LOCAL from within `directory`."""
    return {p.stem for p in directory.glob("*.py")} | {
        p.name for p in directory.iterdir()
        if p.is_dir() and not p.name.startswith((".", "__"))
        and _bears_python(p)
    }


def _third_party_imports() -> set[str]:
    """Top-level module names imported anywhere under the app root that no
    local module provides.

    Locality is judged PER FILE: a script executed from a nested directory
    legitimately imports its siblings, so each file's own directory counts as
    local for that file alone (Sol, v0.222.0 review — a valid sibling import
    must not read as an undeclared dependency). Relative imports carry no
    external name and are skipped.
    """
    root_local = _local_names(APP_ROOT)
    stdlib = set(sys.stdlib_module_names)
    third_party = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        local = root_local | _local_names(path.parent)
        tree = ast.parse(path.read_text(), filename=str(path))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
        third_party |= names - stdlib - local
    return third_party


def test_every_third_party_import_resolves_to_a_declared_dependency():
    third_party = _third_party_imports()
    assert third_party, "the scan found no third-party imports at all — broken scan"

    declared = _declared_distributions()
    providers = packages_distributions()

    undeclared = {}
    for module in sorted(third_party):
        dists = providers.get(module)
        if not dists:
            # Not even installed in this venv — which the manifest builds — so
            # the import cannot work in the image either.
            undeclared[module] = "provided by NO installed distribution"
        elif not {_canonical(d) for d in dists} & declared:
            undeclared[module] = (
                f"provided only by {sorted(set(dists))}, none declared in "
                f"casa/requirements.txt")
    assert not undeclared, (
        "imports that the shipped manifest does not guarantee (a transitive "
        "dependency is a loan, not a declaration): " + repr(undeclared))
