"""Baseline runtime tools (§4.5) must all resolve inside the running addon
container. Run under the `docker` marker — skipped if no docker in env.

The same built image also carries the release build's two launch-path guards
(#957); the tests at the bottom run each guard's own `RUN` body against it with
one launch-path program broken, and require the guard to refuse. They prove the
guards' MECHANISM on this runner's architecture only. No test in this tree can
establish that the published aarch64 image is healthy; that is what a native
aarch64 lane would run (#985).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import uuid
import warnings
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.docker]

BASELINE_TOOLS = [
    "bash", "sh", "curl", "jq", "yq", "git",
    "python3", "pip3", "node", "npm", "gh", "op", "claude",
    "ca-certificates", "ssh", "openssl", "tar", "gzip", "unzip",
]


# One tag per session (#970). A constant tag let two overlapping local
# `make test-docker` runs share it: the second build replaced the image while
# the first run's containers were still asserting against it, under a warning
# that still named the first run's base. The prefix is fixed so a tag leaked by
# a killed run is findable (test-local/README.md has the sweep).
IMAGE_TAG_PREFIX = "casa:local-baseline-"


def unique_image_tag() -> str:
    return f"{IMAGE_TAG_PREFIX}{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def image_tag():
    tag = unique_image_tag()
    yield tag
    # Untags only: the build cache survives, so the next run is not slower.
    # Never fatal, and nothing to do when docker is absent (the build skipped).
    if shutil.which("docker") is not None:
        subprocess.run(["docker", "image", "rm", tag],
                       capture_output=True, check=False)


def build_and_report(image_tag: str) -> None:
    """Build `casa/Dockerfile`, then say which base the build actually used.

    NO `--pull` (#942, #970). This build runs in qa.yml's `baseline-runtime`
    job, and `main` is protected, so resolving the tag here would make a ghcr
    outage or rate-limit a red push gate — the network dependency #937 kept out
    of CI. The base therefore still comes from whatever copy of the floating
    tag is in the local store, which is the defect; what this closes is that
    NOTHING said so. After #942 this was the only building path, local or CI,
    that reported nothing at all, while qa.yml's tier2 and tier3 image builds
    have printed the label since #937.

    A warning rather than a print: pytest shows its warnings summary on a GREEN
    run, and captured fixture output is only surfaced when something fails —
    but the run you cannot attribute to a base is the one that passed.
    """
    subprocess.run(
        ["docker", "build",
         "--build-arg", "BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm",
         "-t", image_tag, "-f", "casa/Dockerfile", "casa/"],
        check=True,
    )
    r = subprocess.run(
        ["docker", "image", "inspect", image_tag,
         "--format", '{{index .Config.Labels "io.hass.version"}}'],
        capture_output=True, text=True,
    )
    base = r.stdout.strip() if r.returncode == 0 else ""
    if base in ("", "<no value>"):
        base = "unknown"
    # Never fatal: a diagnostic that can fail the thing it diagnoses is worse
    # than no diagnostic.
    warnings.warn(
        f"baseline base io.hass.version={base} (this build does not resolve "
        f"the floating tag; freshness not checked here)",
        stacklevel=2,
    )


@pytest.fixture(scope="session", autouse=True)
def _build_image(image_tag: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    build_and_report(image_tag)


@pytest.mark.parametrize("tool", BASELINE_TOOLS)
def test_tool_on_path(tool: str, image_tag: str) -> None:
    # Special-case: ca-certificates is a package, test for its installed file.
    if tool == "ca-certificates":
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image_tag,
             "-c", "test -f /etc/ssl/certs/ca-certificates.crt"],
        )
    else:
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image_tag,
             "-c", f"command -v {tool}"],
        )
    assert r.returncode == 0, f"baseline runtime regression: {tool} missing"


# --------------------------------------------------------------------------
# #957: the release build's launch-path guards refuse a broken program.
# --------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]

# A truncated ELF header with an unknown machine: the kernel refuses it with
# ENOEXEC on any host, including one with a qemu binfmt handler registered.
_BOGUS_ELF = (
    r"printf '\177ELF\002\001\001\000\000\000\000\000\000\000\000\000\002\000\377\377'"
    ' > "$f"; head -c 200 /dev/zero >> "$f"'
)


def _guard_bodies() -> tuple[str, str]:
    """The exec probe's and the compile smoke's RUN bodies, read VERBATIM from
    casa/Dockerfile with the same instruction reader the list pin uses; a copy
    would pin the copy."""
    spec = importlib.util.spec_from_file_location(
        "_casa_launch_path_guard", REPO / "tests" / "test_launch_path_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    text = (REPO / "casa" / "Dockerfile").read_text(encoding="utf-8")
    runs = [i.strip()[len("RUN "):] for i in guard._logical_instructions(text)
            if i.strip().startswith("RUN ")]
    probe = [b for b in runs if "subprocess.run([p]" in b]
    smoke = [b for b in runs if 's6-rc-compile "${scratch}/db"' in b]
    assert len(probe) == 1 and len(smoke) == 1, (probe, smoke)
    assert len(guard.probe_instructions(text)) == 1
    return probe[0], smoke[0]


def _run_in_image(image_tag: str, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--user", "root",
         "--entrypoint", "/bin/bash", image_tag, "-o", "pipefail", "-c", script],
        capture_output=True, text=True,
    )


_TARGET = 'f="$(readlink -f "$(command -v s6-rc-update)")" && '


@pytest.mark.parametrize("mutation", [
    "",
    _TARGET + 'chmod 0644 "$f"',
    _TARGET + _BOGUS_ELF,
    _TARGET + ': > "$f"',
    _TARGET + "printf 'echo hi\\n' > \"$f\"",
    _TARGET + 'rm -f "$f"',
], ids=["healthy", "mode-0644", "bogus-elf", "empty", "no-shebang-text", "removed"])
def test_exec_probe_refuses_an_unlaunchable_program(mutation: str, image_tag: str) -> None:
    probe, _ = _guard_bodies()
    r = _run_in_image(image_tag, f"{mutation}\n{probe}" if mutation else probe)
    if not mutation:
        assert r.returncode == 0, r.stderr
        return
    assert r.returncode != 0, (mutation, r.stdout, r.stderr)
    assert "s6-rc-update" in r.stderr, r.stderr


@pytest.mark.parametrize("mutation", [
    "", "rm /etc/s6-overlay/s6-rc.d/svc-casa/type",
    "rm /etc/s6-overlay/user-bundles.d/user/type",
    "mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d",
], ids=["healthy", "service-type-removed", "user-bundle-type-removed",
        "user-bundle-left-in-service-sources"])
def test_compile_smoke_refuses_a_database_that_does_not_compile(
    mutation: str, image_tag: str,
) -> None:
    _, smoke = _guard_bodies()
    r = _run_in_image(image_tag, f"{mutation}\n{smoke}" if mutation else smoke)
    if not mutation:
        assert r.returncode == 0, r.stderr
        return
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "does not compile" in r.stderr, r.stderr


# --------------------------------------------------------------------------
# The memory store on the image's own SQLite.
# --------------------------------------------------------------------------

_MEMORY_PROBE = """
import sqlite3
from memory.registry import REGISTRY, Kind
from memory.store import MemoryStore, schema_version
store = MemoryStore("/tmp/memory-probe")
assert store.open(), store.unavailable_reason
with store.transaction() as conn:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")}
registered = {t for e in REGISTRY if e.kind is Kind.TABLE for t in e.paths}
assert version == schema_version(), version
assert tables == registered, sorted(tables ^ registered)
print("ok", sqlite3.sqlite_version, version, len(tables))
"""


def test_fresh_memory_store_on_the_image_sqlite(image_tag: str) -> None:
    """The store the image ships, opened on the SQLite its interpreter links:
    the FTS5 check passes and the schema is created, every table registered.
    The dev venv's SQLite is newer than the image's, so only this run speaks
    for the image."""
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-e", "PYTHONPATH=/opt/casa",
         "--entrypoint", "/opt/casa/venv/bin/python3", image_tag,
         "-c", _MEMORY_PROBE],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.startswith("ok "), r.stdout


# --------------------------------------------------------------------------
# The memory wipe's container-start step, in the image.
# --------------------------------------------------------------------------

_WIPE_IN_IMAGE = r"""
set -e
test -x /etc/s6-overlay/scripts/memory-maintenance.sh
test "$(cat /etc/s6-overlay/s6-rc.d/init-memory-maintenance/type)" = oneshot
test -e /etc/s6-overlay/s6-rc.d/init-setup-configs/dependencies.d/init-memory-maintenance
test -e /etc/s6-overlay/user-bundles.d/user/contents.d/init-memory-maintenance
C=5f0c1d2e3a4b5c6d7e8f9a0b
export PYTHONPATH=/opt/casa
/opt/casa/venv/bin/python3 - <<PY
from memory import capture, maintenance
from memory.store import MemoryStore
store = MemoryStore()
assert store.open(), store.unavailable_reason
capture.append(store, capture.event(role="assistant", channel="telegram",
    speaker_kind="operator", origin_kind="operator", text="told $C",
    eligibility="eligible", origin_tier="private"))
import os
os.makedirs("/config/cc-home/.claude/projects/-a", exist_ok=True)
open("/config/cc-home/.claude/projects/-a/s.jsonl", "w").write("$C")
open("/data/sessions.json", "w").write('{"k": "$C"}')
os.makedirs("/data/explanations", exist_ok=True)
open("/data/explanations/abcd1234.json", "w").write("$C")
assert maintenance.write_wipe_intent() == "written"
PY
/opt/casa/venv/bin/python3 -m memory.maintenance
test ! -e /data/memory/maintenance.json
if grep -rl "$C" /data /config /root 2>/dev/null; then exit 3; fi
/opt/casa/venv/bin/python3 -c "from memory.store import MemoryStore; s = MemoryStore(); assert s.open(); print('fresh', s.path)"
"""


def test_the_wipe_step_runs_on_the_image(image_tag: str) -> None:
    """The oneshot ships in the s6 tree, and the step itself wipes a store
    and the memory beside it on the image's own Python and SQLite."""
    r = _run_in_image(image_tag, _WIPE_IN_IMAGE)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "fresh /data/memory/memory.db" in r.stdout, r.stdout
    assert "memory maintenance: wipe done" in r.stderr, r.stderr
