"""The code-derived coverage ledger.

Enumeration comes from the code, never from a hand list; the ledger maps every
enumerated surface to the corpus document that covers it, or records an explicit
exclusion. Both directions are checked, like the manifest.
"""
import importlib.util
from pathlib import Path

# Loaded by explicit path for the same reason as test_verify_docs.py: the app code root
# on sys.path contains its own scripts/ directory which shadows the repo-root one.
_spec = importlib.util.spec_from_file_location(
    "casa_coverage_ledger",
    Path(__file__).resolve().parents[1] / "scripts" / "coverage_ledger.py",
)
coverage_ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coverage_ledger)

BIG_MODULE = "# module\n" + "x = 1\n" * 120
SMALL_MODULE = "x = 1\n"

TOOLS_PY = (
    "def send_message():\n    pass\n\n"
    "def react():\n    pass\n\n"
    "CASA_TOOLS: tuple = (\n    send_message,\n    react,\n)\n"
    + "# padding\n" * 120
)

CONFIG_YAML = """
options:
  log_level: info
schema:
  log_level: str
  new_key: str
"""

ROUTES_PY = (
    "def build(app, dynamic_path):\n"
    "    app.router.add_get('/healthz', h)\n"
    "    app.router.add_post('/invoke/{agent}', h)\n"
    "    app.router.add_post(dynamic_path, h)\n"
    + "# padding\n" * 120
)

MANIFEST = """
- doc: manifest.yaml
  kind: meta
  summary: Allowlist.
- doc: architecture/thing.md
  summary: A doc.
  when_changing: things
"""


def _repo(tmp_path: Path, ledger: str | None = None) -> Path:
    code = tmp_path / "casa" / "rootfs" / "opt" / "casa"
    code.mkdir(parents=True)
    (code / "big.py").write_text(BIG_MODULE)
    (code / "small.py").write_text(SMALL_MODULE)
    (code / "tools.py").write_text(TOOLS_PY)
    (code / "routes_mod.py").write_text(ROUTES_PY)
    (tmp_path / "casa" / "config.yaml").write_text(CONFIG_YAML)
    s6 = tmp_path / "casa" / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
    (s6 / "svc-casa").mkdir(parents=True)
    (s6 / "init-setup").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manifest.yaml").write_text(MANIFEST)
    if ledger is not None:
        (tmp_path / "docs" / "coverage.yaml").write_text(ledger)
    return tmp_path


def _full_ledger_for(root: Path) -> str:
    lines = []
    for item in coverage_ledger.enumerate_items(root):
        lines.append(f"- item: {item}\n  doc: architecture/thing.md\n")
    return "".join(lines)


# --- enumeration is mechanical, from code ---------------------------------------------

def test_enumeration_covers_every_surface_kind(tmp_path):
    items = coverage_ledger.enumerate_items(_repo(tmp_path))
    assert "casa/rootfs/opt/casa/big.py" in items          # every module —
    assert "casa/rootfs/opt/casa/small.py" in items        # no size floor
    assert "option:log_level" in items                     # options: key
    assert "option:new_key" in items                       # schema:-only key still counts
    assert "s6:svc-casa" in items and "s6:init-setup" in items
    assert "tool:send_message" in items and "tool:react" in items
    assert "route:casa/rootfs/opt/casa/routes_mod.py:GET:/healthz" in items
    assert "route:casa/rootfs/opt/casa/routes_mod.py:POST:/invoke/{agent}" in items


def test_enumeration_covers_env_scripts_schemas_and_dockerfile(tmp_path):
    """Env reads are AST-enumerated (a commented read does not count), boot
    scripts, schema files and the Dockerfile are surfaces too."""
    root = _repo(tmp_path)
    code = root / "casa" / "rootfs" / "opt" / "casa"
    (code / "envuser.py").write_text(
        "import os\n"
        'A = os.environ.get("CASA_PROBE_A")\n'
        'B = os.environ["CASA_PROBE_B"]\n'
        'C = os.getenv("CASA_PROBE_C")\n'
        '# os.environ.get("CASA_PROBE_COMMENTED")\n'
        "def _env_int(name, default):\n"
        "    return int(os.environ.get(name, default))\n"
        'D = _env_int("CASA_PROBE_HELPER", 3)\n'
        "class settings:\n"
        "    environ = {}\n"
        'E = settings.environ.get("CASA_PROBE_DECOY")\n'
        'F = settings.environ["CASA_PROBE_DECOY2"]\n'
        "def reader(env=os.environ):\n"
        '    return env.get("CASA_PROBE_PARAM")\n'
        "def cond_reader(env=None):\n"
        "    env = env if env is not None else os.environ\n"
        '    return env["CASA_PROBE_BOUND"]\n'
        "def other(mapping={}):\n"
        '    return mapping.get("CASA_PROBE_NOT_ENV")\n'
        'raw = os.environ.get("CASA_PROBE_A")\n'
        "raw = {}\n"
        'G = raw.get("CASA_PROBE_REUSED_NAME")\n'
        "def crossed(env={}):\n"
        '    return env.get("CASA_PROBE_CROSS_SCOPE")\n'
    )
    scripts = root / "casa" / "rootfs" / "etc" / "s6-overlay" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "setup-probe.sh").write_text("#!/bin/sh\n")
    schema = code / "defaults" / "schema"
    schema.mkdir(parents=True)
    (schema / "probe.v1.json").write_text("{}")
    (root / "casa" / "Dockerfile").write_text("FROM scratch\n")

    items = coverage_ledger.enumerate_items(root)
    assert "env:CASA_PROBE_A" in items
    assert "env:CASA_PROBE_B" in items
    assert "env:CASA_PROBE_C" in items
    assert "env:CASA_PROBE_COMMENTED" not in items
    assert "env:CASA_PROBE_HELPER" in items       # read via an _env_* helper
    assert "env:CASA_PROBE_DECOY" not in items    # not os.environ
    assert "env:CASA_PROBE_DECOY2" not in items
    assert "env:CASA_PROBE_PARAM" in items        # param defaulted to os.environ
    assert "env:CASA_PROBE_BOUND" in items        # name bound from os.environ
    assert "env:CASA_PROBE_NOT_ENV" not in items  # unrelated mapping
    assert "env:CASA_PROBE_REUSED_NAME" not in items  # value-bind, name reused
    # `reader(env=os.environ)` must not contaminate `crossed(env={})`:
    assert "env:CASA_PROBE_CROSS_SCOPE" not in items
    assert "script:setup-probe.sh" in items
    assert "schema:probe.v1.json" in items
    assert "casa/Dockerfile" in items


def test_a_dynamic_route_path_is_enumerated_not_skipped(tmp_path):
    """A registration whose path is a variable is still a surface; skipping it would let
    a whole route family go unledgered."""
    items = coverage_ledger.enumerate_items(_repo(tmp_path))
    assert any(
        i.startswith("route:casa/rootfs/opt/casa/routes_mod.py:POST:") and "dynamic_path" in i
        for i in items
    )


def test_env_read_via_a_module_level_string_constant_is_enumerated(tmp_path):
    """Minor-9 pin: `SPOOL_ROOT_ENV = "CASA_EVENT_SPOOL_ROOT"` then
    `os.environ.get(SPOOL_ROOT_ENV)` — the constant, not the literal,
    passed at the call site — must resolve to the SAME env var name a
    direct literal would. This is exactly the shape that let
    env:CASA_EVENT_SPOOL_ROOT (event_spool.py) and
    env:CASA_CALLBACK_SPOOL_ROOT (callback_spool.py) escape this
    scanner."""
    root = _repo(tmp_path)
    code = root / "casa" / "rootfs" / "opt" / "casa"
    (code / "constuser.py").write_text(
        "import os\n"
        'ROOT_ENV = "CASA_PROBE_INDIRECT"\n'
        "def spool_root():\n"
        "    return os.environ.get(ROOT_ENV)\n"
    )
    items = coverage_ledger.enumerate_items(root)
    assert "env:CASA_PROBE_INDIRECT" in items


# --- the check bites, both directions --------------------------------------------------

def test_a_fully_assigned_ledger_passes(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "coverage.yaml").write_text(_full_ledger_for(root))
    assert coverage_ledger.check(root) == []


def test_an_unassigned_enumerated_item_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n", ""
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert any("option:log_level" in p for p in coverage_ledger.check(root))


def test_a_stale_ledger_entry_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "coverage.yaml").write_text(
        _full_ledger_for(root) + "- item: tool:vanished\n  doc: architecture/thing.md\n"
    )
    assert any("tool:vanished" in p for p in coverage_ledger.check(root))


def test_an_exclusion_with_a_reason_passes(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n",
        '- item: option:log_level\n  excluded: "PHASE-3: reference/operator-options.md"\n',
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert coverage_ledger.check(root) == []


def test_an_exclusion_without_a_reason_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n",
        '- item: option:log_level\n  excluded: ""\n',
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert any("option:log_level" in p and "reason" in p for p in coverage_ledger.check(root))


def test_a_doc_not_in_the_manifest_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = _full_ledger_for(root).replace(
        "- item: option:log_level\n  doc: architecture/thing.md\n",
        "- item: option:log_level\n  doc: architecture/ghost.md\n",
    )
    (root / "docs" / "coverage.yaml").write_text(ledger)
    assert any("architecture/ghost.md" in p for p in coverage_ledger.check(root))


def test_a_duplicate_ledger_item_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "coverage.yaml").write_text(
        _full_ledger_for(root) + "- item: option:log_level\n  doc: architecture/thing.md\n"
    )
    assert any("option:log_level" in p and "twice" in p for p in coverage_ledger.check(root))


def test_a_missing_ledger_is_a_finding_not_a_traceback(tmp_path):
    root = _repo(tmp_path)
    assert any("coverage.yaml" in p for p in coverage_ledger.check(root))


def test_manifest_docs_includes_shard_entries(tmp_path):
    """#367: a coverage assignment to a document manifested only in a
    docs/manifest.d/ shard must be recognized — pre-shard the ledger read only
    the root manifest and would refuse the assignment as unknown."""
    (tmp_path / "docs" / "manifest.d").mkdir(parents=True)
    (tmp_path / "docs" / "manifest.yaml").write_text(
        "- doc: manifest.yaml\n  kind: meta\n  summary: x\n")
    (tmp_path / "docs" / "manifest.d" / "architecture.yaml").write_text(
        "- doc: architecture/x.md\n  summary: y\n")
    docs = coverage_ledger._manifest_docs(tmp_path)
    assert "architecture/x.md" in docs
    assert "manifest.yaml" in docs


# --- where the two ownership maps overlap they must agree (#717, ruled) ---------------

MANIFEST_WITH_COVERS = """
- doc: manifest.yaml
  kind: meta
  summary: Allowlist.
- doc: architecture/thing.md
  summary: A doc.
  when_changing: things
- doc: architecture/other.md
  summary: Another doc.
  when_changing: other things
  covers:
    - casa/rootfs/opt/casa/big.py::SomeClass.method
"""

SHARD_WITH_COVERS = """
- doc: architecture/third.md
  summary: A shard doc.
  when_changing: third things
  covers:
    - casa/rootfs/opt/casa/big.py
"""


def _ledger_with(root, overrides):
    lines = []
    for item in coverage_ledger.enumerate_items(root):
        doc = overrides.get(item, "architecture/thing.md")
        lines.append(f"- item: {item}\n  doc: {doc}\n")
    return "".join(lines)


def test_an_assignment_disagreeing_with_a_covers_claim_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "manifest.yaml").write_text(MANIFEST_WITH_COVERS)
    # big.py is claimed by other.md's covers but assigned to thing.md
    (root / "docs" / "coverage.yaml").write_text(_ledger_with(root, {}))
    problems = coverage_ledger.check(root)
    assert any("big.py" in p and "disagree" in p for p in problems), problems
    # the disagreement names the claimant so the fix is one hop away
    assert any("architecture/other.md" in p for p in problems)


def test_an_assignment_matching_its_covers_claimant_passes(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "manifest.yaml").write_text(MANIFEST_WITH_COVERS)
    (root / "docs" / "coverage.yaml").write_text(
        _ledger_with(root, {"casa/rootfs/opt/casa/big.py": "architecture/other.md"})
    )
    assert coverage_ledger.check(root) == []


def test_a_multi_claimant_item_passes_when_assigned_to_any_claimant(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "manifest.yaml").write_text(MANIFEST_WITH_COVERS)
    shard_dir = root / "docs" / "manifest.d"
    shard_dir.mkdir()
    (shard_dir / "extra.yaml").write_text(SHARD_WITH_COVERS)
    # big.py now has two claimants (other.md by symbol anchor, third.md by bare
    # path from a shard); assignment to the SHARD's claimant must pass — this
    # also pins that claimants are read from manifest.d, not the root alone.
    (root / "docs" / "coverage.yaml").write_text(
        _ledger_with(root, {"casa/rootfs/opt/casa/big.py": "architecture/third.md"})
    )
    assert coverage_ledger.check(root) == []


def test_an_item_no_covers_claims_is_not_judged_by_the_cross_check(tmp_path):
    """The ruled boundary: the predicate fires only where the maps OVERLAP.

    small.py has no covers anchor anywhere; its assignment to thing.md must not
    draw a disagreement — widening the guard's coverage is a separate decision,
    and a cross-check that fired on every unclaimed module would red the build
    on all of them today.
    """
    root = _repo(tmp_path)
    (root / "docs" / "manifest.yaml").write_text(MANIFEST_WITH_COVERS)
    (root / "docs" / "coverage.yaml").write_text(
        _ledger_with(root, {"casa/rootfs/opt/casa/big.py": "architecture/other.md"})
    )
    problems = coverage_ledger.check(root)
    assert not any("small.py" in p for p in problems)
    # namespaced non-path items (option:, s6:, tool:, route:) never key a covers
    # path, so they are structurally outside the cross-check too
    assert not any("option:" in p or "s6:" in p for p in problems)
