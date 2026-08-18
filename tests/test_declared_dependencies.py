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


def _imported_top_levels() -> set[str]:
    """Top-level module names imported anywhere under the app root.

    Relative imports carry no external name and are skipped; absolute ones
    contribute their first dotted component.
    """
    names = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
    return names


def test_every_third_party_import_resolves_to_a_declared_dependency():
    local = {p.stem for p in APP_ROOT.glob("*.py")} | {
        p.name for p in APP_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith((".", "__"))
    }
    third_party = _imported_top_levels() - set(sys.stdlib_module_names) - local
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
