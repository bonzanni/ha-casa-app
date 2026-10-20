"""Which app a push or pull request builds and releases, and under which tag.

The repository carries Casa (`casa/`) and, once it exists, the optional Casa Browser app
(`casa_browser/`). `.github/workflows/deploy.yml` runs one reusable per-app workflow
(`.github/workflows/deploy-app.yml`) for each app `scripts/browser-release-matrix.py`
selects; these tests pin the selection, the tag namespaces and the workflow wiring that
keeps Casa's own release what it was.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "browser-release-matrix.py"

_spec = importlib.util.spec_from_file_location("casa_browser_release_matrix", SCRIPT)
matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(matrix)


# --- selection ------------------------------------------------------------------------

def test_browser_only_release():
    """A change inside casa_browser/ selects only the browser app; a change inside casa/
    selects only Casa; a change to the shared pipeline selects both, for validation."""
    assert matrix.select_apps({"casa_browser/rootfs/opt/casa_browser/api.py"}, False) == [
        "casa_browser"
    ]
    assert matrix.select_apps({"casa/config.yaml"}, False) == ["casa"]
    assert matrix.select_apps({"casa/x.py", "casa_browser/y.py"}, False) == [
        "casa", "casa_browser"
    ]
    assert matrix.select_apps({"docs/README.md"}, False) == []
    assert matrix.select_apps(set(), True) == ["casa", "casa_browser"]
    # a folder whose name merely STARTS with an app's name is not that app
    assert matrix.select_apps({"casa_browserx/a.py", "casax/b.py"}, False) == []


def test_release_tags_are_namespaced_per_app():
    assert matrix.release_tag("casa", "1.2.3") == "v1.2.3"
    assert matrix.release_tag("casa_browser", "0.1.0") == "casa_browser-v0.1.0"
    with pytest.raises(KeyError):
        matrix.release_tag("nope", "1.0.0")


def test_casa_release_settings_are_the_pre_matrix_ones():
    """Casa keeps the unprefixed tag, gh's own Latest decision and the builder's default
    cache scope; only the second app opts out of Latest and scopes its cache."""
    casa = matrix.release_settings("casa", "0.344.95")
    assert casa == {"tag": "v0.344.95", "latest_flag": "", "cache_scope": ""}
    browser = matrix.release_settings("casa_browser", "0.1.0")
    assert browser == {
        "tag": "casa_browser-v0.1.0",
        "latest_flag": "--latest=false",
        "cache_scope": "casa_browser",
    }


# --- the CLI against a real git history -------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "harness")
    _git(work, "config", "user.name", "harness")
    (work / "casa").mkdir()
    (work / "casa" / "config.yaml").write_text('name: "Casa"\nversion: "0.344.95"\n')
    (work / "docs").mkdir()
    (work / "docs" / "x.md").write_text("x\n")
    _commit_all(work, "base")
    return work


def _select(repo: Path, base: str, head: str) -> list[dict]:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "select", "--base", base, "--head", head,
         "--repo-root", str(repo)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def test_cli_an_absent_app_is_never_selected(repo):
    """Until casa_browser/ exists, nothing selects it — not even a shared pipeline change —
    so the second app cannot build or publish anything."""
    base = _git(repo, "rev-parse", "HEAD")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy-app.yml").write_text("on: {}\n")
    head = _commit_all(repo, "shared pipeline change")
    assert _select(repo, base, head) == [{"app": "casa", "version": "0.344.95"}]


def test_cli_selects_only_the_changed_app(repo):
    (repo / "casa_browser").mkdir()
    (repo / "casa_browser" / "config.yaml").write_text("version: 0.1.0\n")
    base = _commit_all(repo, "add the second app")
    (repo / "casa_browser" / "DOCS.md").write_text("docs\n")
    head = _commit_all(repo, "browser change")
    assert _select(repo, base, head) == [{"app": "casa_browser", "version": "0.1.0"}]

    (repo / "docs" / "x.md").write_text("y\n")
    other = _commit_all(repo, "docs only")
    assert _select(repo, head, other) == []


def test_cli_an_unknown_base_validates_every_present_app(repo):
    """A push whose `before` is all zeros (or a base this clone lacks) cannot be diffed;
    every present app is selected rather than none."""
    head = _git(repo, "rev-parse", "HEAD")
    zeros = "0" * 40
    assert _select(repo, zeros, head) == [{"app": "casa", "version": "0.344.95"}]
    assert _select(repo, "", head) == [{"app": "casa", "version": "0.344.95"}]


def test_cli_release_prints_github_output_lines(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "release", "--app", "casa_browser",
         "--version", "0.1.0"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert out == ["tag=casa_browser-v0.1.0", "latest_flag=--latest=false",
                   "cache_scope=casa_browser"]


def test_manifest_version_reader_handles_quoting():
    assert matrix.manifest_version('version: "0.344.95"\n') == "0.344.95"
    assert matrix.manifest_version("name: x\nversion: 0.1.0  # c\n") == "0.1.0"
    assert matrix.manifest_version("  version: 9\n") is None  # nested key, not top level


# --- the workflows ----------------------------------------------------------------------

def _workflow(name: str) -> dict:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    # PyYAML reads the bare key `on` as boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_deploy_triggers_on_both_apps_and_the_shared_pipeline():
    """Both PR and push contexts watch both app folders and every shared pipeline file,
    and the script's shared list names exactly those pipeline files."""
    on = _workflow("deploy.yml")["on"]
    expected = {f"{app}/**" for app in matrix.APPS} | set(matrix.SHARED_PATHS)
    assert set(on["pull_request"]["paths"]) == expected
    assert set(on["push"]["paths"]) == expected
    assert on["push"]["branches"] == ["main"]


def test_deploy_calls_the_per_app_workflow_from_a_selected_matrix():
    jobs = _workflow("deploy.yml")["jobs"]
    deploy = jobs["deploy"]
    assert deploy["uses"] == "./.github/workflows/deploy-app.yml"
    assert deploy["with"]["app"] == "${{ matrix.app }}"
    assert "fromJSON(needs.select.outputs.matrix)" in str(deploy["strategy"]["matrix"])
    # an empty selection runs nothing, rather than failing on an empty matrix
    assert "needs.select.outputs.matrix != '[]'" in deploy["if"]
    # per-app (and per-version) concurrency; only pull requests cancel in progress
    concurrency = deploy["concurrency"]
    assert "matrix.app" in concurrency["group"] and "matrix.version" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_per_app_workflow_publishes_only_on_push_and_only_an_unpublished_version():
    wf = _workflow("deploy-app.yml")
    assert set(wf["on"]) == {"workflow_call"}
    jobs = wf["jobs"]
    prepare_steps = "\n".join(str(s) for s in jobs["prepare"]["steps"])
    assert "github.event_name" in prepare_steps and "ls-remote" in prepare_steps
    build = jobs["build"]["steps"][-1]["with"]
    assert build["push"] == "${{ needs.prepare.outputs.publish == 'true' }}"
    assert build["context"] == "./${{ inputs.app }}"
    for job in ("manifest", "release"):
        assert jobs[job]["if"] == "needs.prepare.outputs.publish == 'true'"
    release = "\n".join(str(s) for s in jobs["release"]["steps"])
    assert '--target "${GITHUB_SHA}"' in release
    assert "${LATEST_FLAG}" in release


def test_the_docs_impact_version_exemption_knows_every_app():
    """scripts/docs_impact.sh judges each app manifest's version-only bump on its own;
    its list of manifests must be exactly the release pipeline's apps."""
    text = (ROOT / "scripts" / "docs_impact.sh").read_text()
    loops = [ln for ln in text.splitlines() if ln.startswith("for manifest in ")]
    assert len(loops) == 1, loops
    listed = loops[0].removeprefix("for manifest in ").removesuffix("; do").split()
    assert listed == [f"{app}/config.yaml" for app in matrix.APPS]


def test_the_ledger_knows_every_app():
    """The coverage ledger enumerates the same app folders the release pipeline ships."""
    spec = importlib.util.spec_from_file_location(
        "casa_coverage_ledger_for_matrix", ROOT / "scripts" / "coverage_ledger.py")
    ledger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ledger)
    assert [app.folder for app in ledger.APPS] == list(matrix.APPS)


def test_the_app_linter_runs_once_per_app():
    legs = _workflow("lint.yml")["jobs"]["addon-linter"]["strategy"]["matrix"]["app"]
    assert legs == list(matrix.APPS)


# --- the publish decision, executed -----------------------------------------------------

def _publish_step() -> str:
    steps = _workflow("deploy-app.yml")["jobs"]["prepare"]["steps"]
    found = [s for s in steps if s.get("id") == "release"]
    assert len(found) == 1, found
    return found[0]["run"]


def _decide(tmp_path: Path, event: str, origin: str, app: str = "casa",
            version: str = "0.344.95") -> tuple[int, dict[str, str], str]:
    """Run the workflow's own publish-decision shell in a throwaway clone whose
    `origin` is ``origin``; return its exit status and the GITHUB_OUTPUT it wrote."""
    work = tmp_path / f"work-{event}-{len(list(tmp_path.iterdir()))}"
    work.mkdir()
    (work / "scripts").mkdir()
    (work / "scripts" / SCRIPT.name).write_text(SCRIPT.read_text())
    _git(work, "init", "-q")
    _git(work, "remote", "add", "origin", origin)
    out_file = work / "github_output"
    out_file.write_text("")
    result = subprocess.run(
        ["bash", "-e", "-c", _publish_step()], cwd=work, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "APP": app,
             "VERSION": version, "EVENT_NAME": event, "GITHUB_OUTPUT": str(out_file),
             "GIT_CONFIG_NOSYSTEM": "1"},
    )
    outputs = dict(line.split("=", 1) for line in out_file.read_text().splitlines() if line)
    return result.returncode, outputs, result.stdout + result.stderr


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare origin holding one commit and the tag v0.344.94 (only)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "harness")
    _git(seed, "config", "user.name", "harness")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "base")
    _git(seed, "tag", "v0.344.94")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


def test_a_push_publishes_an_unreleased_version(tmp_path, origin):
    rc, out, log = _decide(tmp_path, "push", str(origin))
    assert rc == 0, log
    assert out["publish"] == "true", log
    assert out["tag"] == "v0.344.95"
    assert out["latest_flag"] == "" and out["cache_scope"] == ""


def test_a_push_of_a_released_version_publishes_nothing(tmp_path, origin):
    rc, out, log = _decide(tmp_path, "push", str(origin), version="0.344.94")
    assert rc == 0, log
    assert out["publish"] == "false", log


def test_a_pull_request_never_publishes(tmp_path, origin):
    rc, out, log = _decide(tmp_path, "pull_request", str(origin))
    assert rc == 0, log
    assert out["publish"] == "false", log


def test_an_unreadable_origin_stops_the_run(tmp_path):
    """A failed tag lookup is neither 'released' nor 'not released': the run fails."""
    rc, out, log = _decide(tmp_path, "push", str(tmp_path / "no-such-origin.git"))
    assert rc != 0, log
    assert "publish" not in out, log


def test_the_second_app_publishes_under_its_own_tag(tmp_path, origin):
    rc, out, log = _decide(tmp_path, "push", str(origin), app="casa_browser",
                           version="0.1.0")
    assert rc == 0, log
    assert out == {"tag": "casa_browser-v0.1.0", "latest_flag": "--latest=false",
                   "cache_scope": "casa_browser", "publish": "true"}
