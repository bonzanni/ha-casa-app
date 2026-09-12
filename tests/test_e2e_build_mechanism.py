"""How the e2e test image gets built — the argv, not the file's text (#942).

`test-local/Dockerfile.test` mirrors `casa/Dockerfile`'s FLOATING base rather
than pinning a digest (#937), and nothing in the tree resolved that tag: a warm
local image store silently decided which base a local tier certified. Two
harness runs were spent measuring restart-survival against a base three months
older than the released one.

**What these tests pin, stated narrowly enough to be true: the MECHANISM.**
That `make`'s build recipes pass docker's pull flag, that `test-tier3` builds
through that recipe like its neighbours, and that `build_image` reports the base
the image actually carries. They do NOT — and no test in this tree can —
establish the freshness property itself, that a build used the base the registry
serves today: that needs a registry which re-tags.
`tests/test_build_from_parity.py` disclaims exactly this in its own docstring,
and #942 is the gap it disclaims. A reader must not take a green run here as
evidence that any base was current.

They also pin the two DELIBERATE exclusions, which are the half of the design a
later edit is most likely to "fix":

* `build_image` itself takes NO pull flag. `qa.yml`'s tier1/tier2/tier3 steps
  run these harness scripts, and tier2 is the push gate on a protected `main`,
  so a mandatory registry resolution there turns a ghcr outage or rate-limit
  into a red `main`. Refresh belongs on the `make` paths, which CI never
  invokes (`test-local/README.md`).
* the mock-CLI derivative build takes no pull flag either: its base is the
  local-only tag `casa-test`, which no registry serves, so a pull there fails
  deterministically.

Hermetic: a recording `docker` stub on `PATH` plus make's own recipe
resolution. No daemon, no network, no image, and nothing is written into the
repository — which is also why it survives the read-only materialization the
candidate gate runs on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The stub records one line per invocation, NUL-separated argv, on a private
# descriptor — not stdout, which `build_image` suppresses and harnesses parse.
_STUB = """#!/usr/bin/env bash
{ printf '%s\\0' "$@"; printf '\\n'; } >> "$DOCKER_ARGV_LOG"
if [ "${1:-}" = image ] && [ "${2:-}" = inspect ]; then
    printf '%s\\n' "${STUB_INSPECT_OUT-2099.12.probe}"
    exit "${STUB_INSPECT_RC-0}"
fi
exit "${STUB_BUILD_RC-0}"
"""


@pytest.fixture
def stub(tmp_path):
    """A `docker` on PATH that records argv and answers `image inspect`."""
    if shutil.which("bash") is None:      # pragma: no cover - dev env sanity
        pytest.skip("bash is required to exercise the harness helpers")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(_STUB, encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "argv.log"
    log.write_text("", encoding="utf-8")

    class Stub:
        path = bindir
        argv_log = log

        def env(self, **extra):
            e = dict(os.environ)
            e["PATH"] = f"{bindir}{os.pathsep}{e['PATH']}"
            e["DOCKER_ARGV_LOG"] = str(log)
            e["TMPDIR"] = str(tmp_path)
            e.pop("MAKEFLAGS", None)
            e.update(extra)
            return e

        def calls(self) -> list[list[str]]:
            out = []
            for line in log.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                out.append([a for a in line.split("\0") if a != ""])
            return out

    return Stub()


def _builds(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c and c[0] == "build"]


def _run(cmd, stub, **envkw):
    return subprocess.run(
        cmd, cwd=REPO, env=stub.env(**envkw), shell=isinstance(cmd, str),
        capture_output=True, text=True, timeout=300,
    )


def _dry_run(target: str, makedir: Path, stub, *args: str) -> list[str]:
    """Resolve a target's recipe WITHOUT executing it (`make -n`)."""
    proc = _run(["make", "-n", "--no-print-directory", "-C", str(makedir),
                 target, *args], stub)
    assert proc.returncode == 0, proc.stderr
    assert stub.calls() == [], "make -n must not execute anything"
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _build_lines(recipe: list[str]) -> list[str]:
    return [ln for ln in recipe if "docker build" in ln]


def _exec_line(line: str, stub, **envkw):
    proc = _run(line, stub, **envkw)
    assert proc.returncode == 0, proc.stderr
    return proc


# --------------------------------------------------------------------------
# 1. The two `make` build recipes resolve the floating base by default.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "target,makedir",
    [("test-image", REPO), ("build", REPO / "test-local")],
)
def test_make_build_recipe_passes_pull_once(target, makedir, stub):
    recipe = _dry_run(target, makedir, stub)
    lines = _build_lines(recipe)
    assert len(lines) == 1, recipe

    _exec_line(lines[0], stub)
    builds = _builds(stub.calls())
    assert len(builds) == 1, stub.calls()
    argv = builds[0]
    assert argv.count("--pull") == 1, argv
    assert argv.count("-f") == 1 and "test-local/Dockerfile.test" in argv, argv
    assert argv[-1] == ".", argv


@pytest.mark.parametrize(
    "target,makedir",
    [("test-image", REPO), ("build", REPO / "test-local")],
)
def test_make_build_recipe_honours_the_offline_opt_out(target, makedir, stub):
    """`DOCKER_PULL=` is the documented way to build from a warm store.

    A pull fails the build outright when the base cannot be resolved, even
    though the image is present locally. Hard-coding the flag would take a
    developer's ability to build offline with no way back.
    """
    recipe = _dry_run(target, makedir, stub, "DOCKER_PULL=")
    lines = _build_lines(recipe)
    assert len(lines) == 1, recipe

    _exec_line(lines[0], stub)
    builds = _builds(stub.calls())
    assert len(builds) == 1, stub.calls()
    assert builds[0].count("--pull") == 0, builds[0]


# --------------------------------------------------------------------------
# 2. tier3 builds first, like its neighbours.
# --------------------------------------------------------------------------

def test_tier3_builds_before_it_runs_its_harnesses(stub):
    """tier3 was the one tier with no `build` prerequisite — and the one whose
    wasted runs were measured in #942. Its image otherwise comes straight from
    `build_image_with_mock_cli`, which never resolves the base.
    """
    recipe = _dry_run("test-tier3", REPO / "test-local", stub)
    lines = _build_lines(recipe)
    assert len(lines) == 1, recipe

    harness = [i for i, ln in enumerate(recipe) if "test-local/e2e/" in ln]
    assert harness, recipe
    assert recipe.index(lines[0]) < harness[0], recipe

    _exec_line(lines[0], stub)
    builds = _builds(stub.calls())
    assert len(builds) == 1, stub.calls()
    assert builds[0].count("--pull") == 1, builds[0]


# --------------------------------------------------------------------------
# 3. `build_image` reports the base it got — on stderr, and never fatally.
# --------------------------------------------------------------------------

def _build_image(stub, **envkw):
    return _run(["bash", "-c", "source test-local/e2e/common.sh; build_image"],
                stub, IMAGE="probe-image", **envkw)


def test_build_image_reports_the_base_version_on_stderr(stub):
    proc = _build_image(stub)
    assert proc.returncode == 0, proc.stderr

    calls = stub.calls()
    assert len(_builds(calls)) == 1, calls
    inspects = [c for c in calls if c[:2] == ["image", "inspect"]]
    assert len(inspects) == 1, calls
    assert "probe-image" in inspects[0], inspects[0]
    # The FORMAT EXPRESSION, compared whole. "an argument somewhere containing
    # io.hass.version" is not enough: `--format '{{.Id}} io.hass.version'`
    # satisfies that and reports an image ID against real docker, while the
    # recording stub — which cannot evaluate a Go template — answers it
    # identically. The label read is the mechanism, so it is what is asserted.
    assert "--format" in inspects[0], inspects[0]
    fmt = inspects[0][inspects[0].index("--format") + 1]
    assert fmt == '{{index .Config.Labels "io.hass.version"}}', fmt

    reports = [ln for ln in proc.stderr.splitlines() if "io.hass.version" in ln]
    assert len(reports) == 1, proc.stderr
    assert "2099.12.probe" in reports[0], reports[0]
    assert proc.stdout == "", proc.stdout


def test_build_image_passes_no_pull_flag(stub):
    """Deliberate: `build_image` is reached from `qa.yml`'s tier1/2/3 jobs."""
    proc = _build_image(stub)
    assert proc.returncode == 0, proc.stderr
    builds = _builds(stub.calls())
    assert len(builds) == 1, stub.calls()
    assert builds[0].count("--pull") == 0, builds[0]


@pytest.mark.parametrize(
    "inspect_out,inspect_rc",
    [("", "0"), ("<no value>", "0"), ("", "1")],
)
def test_an_unreadable_label_reports_unknown_and_does_not_fail_the_build(
    inspect_out, inspect_rc, stub,
):
    proc = _build_image(stub, STUB_INSPECT_OUT=inspect_out,
                        STUB_INSPECT_RC=inspect_rc)
    assert proc.returncode == 0, proc.stderr
    reports = [ln for ln in proc.stderr.splitlines() if "io.hass.version" in ln]
    assert len(reports) == 1, proc.stderr
    assert "unknown" in reports[0], reports[0]
    assert proc.stdout == "", proc.stdout


def test_a_failed_build_stops_before_the_report(stub):
    proc = _build_image(stub, STUB_BUILD_RC="1")
    assert proc.returncode != 0
    assert [c for c in stub.calls() if c[:2] == ["image", "inspect"]] == []
    assert [ln for ln in proc.stderr.splitlines()
            if "io.hass.version" in ln] == []


# --------------------------------------------------------------------------
# 4. The derivative mock-CLI build must never pull. Green at the base: this is
#    a regression control, not a red case.
# --------------------------------------------------------------------------

def test_the_mock_cli_derivative_build_never_pulls(stub):
    """Its `FROM` is the local-only tag `casa-test`; no registry serves it, so
    a pull there fails deterministically — offline or not.
    """
    proc = _run(["bash", "-c",
                 "source test-local/e2e/common.sh; build_image_with_mock_cli"],
                stub, IMAGE="probe-image")
    assert proc.returncode == 0, proc.stderr

    builds = _builds(stub.calls())
    assert len(builds) == 2, stub.calls()
    assert [b.count("--pull") for b in builds] == [0, 0], builds
    assert "BASE=probe-image" in builds[1], builds[1]
