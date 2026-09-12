"""Baseline runtime tools (§4.5) must all resolve inside the running addon
container. Run under the `docker` marker — skipped if no docker in env."""
from __future__ import annotations

import shutil
import subprocess
import warnings

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.docker]

BASELINE_TOOLS = [
    "bash", "sh", "curl", "jq", "yq", "git",
    "python3", "pip3", "node", "npm", "gh", "op", "claude",
    "ca-certificates", "ssh", "openssl", "tar", "gzip", "unzip",
]


@pytest.fixture(scope="session")
def image_tag() -> str:
    return "casa:local-baseline"


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
