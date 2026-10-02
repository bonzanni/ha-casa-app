"""Mock-SDK drift guard.

The e2e tiers (test-local/Dockerfile.test) force-install
``test-local/mock-claude-sdk`` over the real claude-agent-sdk. Every name
the app imports from ``claude_agent_sdk`` at module scope, and every
keyword ``agent._build_options`` passes to ``ClaudeAgentOptions``, must
exist in the mock — otherwise the container crashes at boot in CI while
the unit gate (which runs against the REAL SDK) stays green. That exact
failure shipped twice: plugins= (v0.5.9 era, see the mock's inline
comment) and StreamEvent/include_partial_messages (v0.67.0, QA run
29160219650). This test moves the failure into the local gate.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_APP = _REPO / "casa" / "rootfs" / "opt" / "casa"
_MOCK_ROOT = _REPO / "test-local" / "mock-claude-sdk"
_MOCK = _MOCK_ROOT / "claude_agent_sdk" / "__init__.py"


def _load_mock():
    spec = importlib.util.spec_from_file_location("_mock_claude_agent_sdk", _MOCK)
    mod = importlib.util.module_from_spec(spec)
    # Register under the THROWAWAY name only (dataclass creation resolves
    # cls.__module__ via sys.modules on CPython 3.12); the real
    # sys.modules["claude_agent_sdk"] used by the rest of the suite stays
    # untouched.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _is_sdk(name: str | None) -> bool:
    return name == "claude_agent_sdk" or (
        name is not None and name.startswith("claude_agent_sdk."))


def _module_level_sdk_imports(path: Path) -> list[tuple[int, str]]:
    """``(line, statement)`` for every ``claude_agent_sdk`` import the module
    executes when it is imported: the module body and any module-level
    ``if``/``try``/``with``/``for``/``while``/``class`` body, submodules and
    plain ``import`` included. Function and lambda bodies are lazy and exempt.
    Unrelated aliases of a mixed ``import`` statement are dropped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                continue
            if isinstance(child, ast.ImportFrom):
                if child.level == 0 and _is_sdk(child.module):
                    found.append((child.lineno, ast.unparse(child)))
            elif isinstance(child, ast.Import):
                aliases = [a for a in child.names if _is_sdk(a.name)]
                if aliases:
                    found.append(
                        (child.lineno, ast.unparse(ast.Import(names=aliases))))
            else:
                visit(child)

    visit(tree)
    return found


# Runs in a child interpreter with no site-packages (-S) and the mock root
# first on its path, so the mock is the only claude_agent_sdk it can import —
# exactly the e2e image after its force-reinstall. Executes ONLY the collected
# import statements, never an application module.
_CHILD = r"""
import json, os, sys
search_root, origin_root = sys.argv[1], os.path.realpath(sys.argv[2])
sys.path.insert(0, search_root)
items = json.loads(sys.stdin.read())
report = {"attempted": 0, "succeeded": 0, "import_errors": [],
          "invalid_origins": []}
flagged = set()
for item in items:
    report["attempted"] += 1
    try:
        exec(item["stmt"], {})
    except Exception as exc:
        report["import_errors"].append(
            {**item, "error": f"{type(exc).__name__}: {exc}"})
    else:
        report["succeeded"] += 1
    for name, mod in list(sys.modules.items()):
        if not (name == "claude_agent_sdk"
                or name.startswith("claude_agent_sdk.")) or name in flagged:
            continue
        f = getattr(mod, "__file__", None)
        # A namespace package (no __init__.py) has no __file__, and
        # setuptools' find_packages would not install it in the image.
        if not (f and os.path.isfile(f) and os.path.realpath(f).startswith(
                origin_root + os.sep)):
            flagged.add(name)
            report["invalid_origins"].append(
                {**item, "module": name, "file": f})
print(json.dumps(report))
"""


def _execute_against_mock(items: list[dict], search_root: Path,
                          origin_root: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", _CHILD,
         str(search_root), str(origin_root)],
        input=json.dumps(items), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def _build_options_kwargs() -> set[str]:
    tree = ast.parse((_APP / "agent.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "ClaudeAgentOptions":
                return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError("no ClaudeAgentOptions(...) call found in agent.py")


def test_every_import_time_sdk_import_resolves_in_the_mock():
    """#1162: a submodule import (``claude_agent_sdk._internal.sessions``) at
    module scope crashed the e2e container at boot while the old name-only
    check, which saw only ``from claude_agent_sdk import …``, stayed green."""
    items = [
        {"file": str(py.relative_to(_REPO)), "line": line, "stmt": stmt}
        for py in sorted(_APP.rglob("*.py"))
        for line, stmt in _module_level_sdk_imports(py)
    ]
    assert items, "no module-scope claude_agent_sdk import found in the app"
    report = _execute_against_mock(items, _MOCK_ROOT, _MOCK_ROOT)
    assert report["attempted"] == len(items), report
    assert report["import_errors"] == [] and report["invalid_origins"] == [], (
        f"the e2e mock cannot satisfy an import the app runs at module scope: "
        f"{report['import_errors'] + report['invalid_origins']} — add the "
        "module/name under test-local/mock-claude-sdk/ (a package dir needs "
        "its __init__.py) or make the import lazy, or the e2e container will "
        "crash at boot (mock-SDK drift)"
    )
    assert report["succeeded"] == len(items), report


_COLLECTOR_FIXTURE = '''
import os, claude_agent_sdk as sdk, json
import claude_agent_sdk.types
from claude_agent_sdk import Item
from claude_agent_sdk.sub.mod import Item as Other
from .claude_agent_sdk import Relative
from claude_agent_sdk_other import NotIt
if os.name:
    from claude_agent_sdk.a import A
try:
    from claude_agent_sdk.b import B
except ImportError:
    from claude_agent_sdk.c import C
finally:
    import claude_agent_sdk.d
with open(__file__) as fh:
    from claude_agent_sdk.e import E
for _ in ():
    from claude_agent_sdk.f import F
while False:
    from claude_agent_sdk.g import G
class Sample:
    from claude_agent_sdk.h import H
    def method(self):
        from claude_agent_sdk.no1 import N
def plain():
    from claude_agent_sdk.no2 import N
    import claude_agent_sdk.no3
async def coro():
    from claude_agent_sdk.no4 import N
'''


def test_collector_sees_every_import_time_sdk_import(tmp_path):
    src = tmp_path / "sample.py"
    src.write_text(_COLLECTOR_FIXTURE, encoding="utf-8")
    got = _module_level_sdk_imports(src)
    assert [stmt for _, stmt in got] == [
        "import claude_agent_sdk as sdk",
        "import claude_agent_sdk.types",
        "from claude_agent_sdk import Item",
        "from claude_agent_sdk.sub.mod import Item as Other",
        "from claude_agent_sdk.a import A",
        "from claude_agent_sdk.b import B",
        "from claude_agent_sdk.c import C",
        "import claude_agent_sdk.d",
        "from claude_agent_sdk.e import E",
        "from claude_agent_sdk.f import F",
        "from claude_agent_sdk.g import G",
        "from claude_agent_sdk.h import H",
    ]
    assert len(got) == 12


def _fake_sdk(root: Path, *, namespace_sub: bool) -> None:
    pkg = root / "claude_agent_sdk"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("Item = 1\n", encoding="utf-8")
    if not namespace_sub:
        (pkg / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub" / "mod.py").write_text("Item = 2\n", encoding="utf-8")


def test_executor_rejects_a_namespace_package(tmp_path):
    _fake_sdk(tmp_path, namespace_sub=True)
    items = [{"file": "sample.py", "line": 1,
              "stmt": "from claude_agent_sdk.sub.mod import Item"}]
    report = _execute_against_mock(items, tmp_path, tmp_path)
    assert report["succeeded"] == 1 and report["import_errors"] == []
    assert [r["module"] for r in report["invalid_origins"]] == [
        "claude_agent_sdk.sub"]


def test_executor_rejects_modules_from_outside_the_mock(tmp_path):
    elsewhere, mock = tmp_path / "elsewhere", tmp_path / "mock"
    _fake_sdk(elsewhere, namespace_sub=False)
    mock.mkdir()
    items = [{"file": "sample.py", "line": 1,
              "stmt": "from claude_agent_sdk.sub.mod import Item"}]
    report = _execute_against_mock(items, elsewhere, mock)
    assert report["succeeded"] == 1
    assert len(report["invalid_origins"]) == 3, report
    clean = _execute_against_mock(items, elsewhere, elsewhere)
    assert clean["succeeded"] == 1 and clean["invalid_origins"] == []


def test_executor_reports_a_missing_module(tmp_path):
    _fake_sdk(tmp_path, namespace_sub=False)
    items = [{"file": "sample.py", "line": 7,
              "stmt": "from claude_agent_sdk._internal.sessions import X"}]
    report = _execute_against_mock(items, tmp_path, tmp_path)
    assert report["attempted"] == 1 and report["succeeded"] == 0
    assert len(report["import_errors"]) == 1
    assert "claude_agent_sdk._internal" in report["import_errors"][0]["error"]

def test_the_mock_installs_the_lookup_the_transcript_reaper_imports():
    """#1162: the reaper imports the SDK's private cwd -> project-dir lookup
    lazily, per pass, so the import-time guard above does not see it. In the
    e2e image the mock must still provide it from a real package (a dir
    without ``__init__.py`` is not installed), or the reaper only ever logs
    that the lookup is unavailable there."""
    items = [{"file": "casa/rootfs/opt/casa/engagement_transcript_reaper.py",
              "line": 0,
              "stmt": "from claude_agent_sdk._internal.sessions import "
                      "_canonicalize_path, _find_project_dir"}]
    report = _execute_against_mock(items, _MOCK_ROOT, _MOCK_ROOT)
    assert report["succeeded"] == 1, report
    assert report["import_errors"] == [] and report["invalid_origins"] == [], report


def test_mock_options_accept_every_kwarg_build_options_passes():
    mock = _load_mock()
    fields = {f.name for f in dataclasses.fields(mock.ClaudeAgentOptions)}
    passed = _build_options_kwargs()
    gaps = passed - fields
    assert not gaps, (
        f"mock ClaudeAgentOptions lacks fields agent._build_options passes: "
        f"{gaps} — every new kwarg needs a mock field (default that ignores it)"
    )


def test_mock_client_adopts_a_chosen_session_id(tmp_path, monkeypatch):
    """#1168: the delegation runner launches under a Casa-chosen
    ``session_id`` and deletes that session at the end, so the e2e mock must
    name its session the way the CLI does — the chosen id, unless resuming."""
    mock = _load_mock()
    monkeypatch.setattr(mock, "CALL_LOG", str(tmp_path / "calls.jsonl"))
    chosen = "44444444-4444-4444-8444-444444444444"
    opts = mock.ClaudeAgentOptions(session_id=chosen,
                                   extra_args={"no-session-persistence": None})
    assert mock.ClaudeSDKClient(opts).session_id == chosen
    resumed = mock.ClaudeAgentOptions(resume="sess-r", session_id=chosen)
    assert mock.ClaudeSDKClient(resumed).session_id == "sess-r"
    assert mock.ClaudeSDKClient(
        mock.ClaudeAgentOptions()).session_id.startswith("mock-")
    # Independent defaults: one options object's extra_args is not another's.
    assert mock.ClaudeAgentOptions().extra_args == {}
    assert mock.ClaudeAgentOptions().extra_args is not \
        mock.ClaudeAgentOptions().extra_args


def test_mock_exports_streamevent_with_event_payload():
    mock = _load_mock()
    ev = mock.StreamEvent(event={"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": "x"}})
    assert ev.event["delta"]["text"] == "x"
    assert "StreamEvent" in mock.__all__


# (g) v0.69.7: SDK dataclasses the app CONSTRUCTS and returns to the SDK must
# mirror the real SDK's field shape EXACTLY — a missing field (the
# PermissionResultDeny.behavior gap, caught by review not the guard) makes the
# offline permission path silently wrong. Distinct from the two guards above,
# which check import-name presence and ClaudeAgentOptions kwargs (name/kwarg
# level), not field shape.
_RESULT_TYPES_MIRRORING_REAL = ["PermissionResultDeny"]


def test_mock_result_dataclasses_mirror_real_sdk_field_shape():
    import claude_agent_sdk as real  # the REAL installed SDK under the unit gate

    mock = _load_mock()
    for name in _RESULT_TYPES_MIRRORING_REAL:
        real_obj = getattr(real, name, None)
        mock_obj = getattr(mock, name, None)
        assert real_obj is not None, (
            f"real SDK no longer exposes {name} — update the guard list"
        )
        assert mock_obj is not None, f"mock SDK lacks {name}"
        assert dataclasses.is_dataclass(real_obj), f"real {name} is not a dataclass"
        assert dataclasses.is_dataclass(mock_obj), f"mock {name} is not a dataclass"
        real_fields = {f.name for f in dataclasses.fields(real_obj)}
        mock_fields = {f.name for f in dataclasses.fields(mock_obj)}
        assert mock_fields == real_fields, (
            f"mock {name} field shape drifted from the real SDK — "
            f"missing={real_fields - mock_fields}, extra={mock_fields - real_fields}; "
            "the e2e container returns this to the SDK, so the shapes must match"
        )
