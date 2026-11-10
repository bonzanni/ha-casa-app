"""The script runner's files in the built image (``casa/Dockerfile``): the dedicated
interpreter is a real file at its exact path, the prelude is beside it, exactly one
sentinel exists and it is named by the committed profile text's digest, and setpriv is
there. Runs under the ``docker`` marker; the image build is shared with
``test_baseline_runtime_assert.py``'s pattern (a unique tag per session, untagged after).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker]

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "casa" / "apparmor.txt"
DIGEST_SCRIPT = REPO / "casa" / "rootfs" / "opt" / "casa" / "scripts" / "pyrun_digest.py"
IMAGE_TAG_PREFIX = "casa:local-run-"


@pytest.fixture(scope="module")
def image_tag():
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    tag = f"{IMAGE_TAG_PREFIX}{uuid.uuid4().hex[:12]}"
    subprocess.run(
        ["docker", "build",
         "--build-arg", "BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm",
         "-t", tag, "-f", "casa/Dockerfile", "casa/"],
        check=True, cwd=REPO)
    yield tag
    subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False)


def _sh(image_tag: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/sh", image_tag,
                           "-c", command], capture_output=True, text=True)


def test_the_box_has_its_interpreter_prelude_and_one_sentinel(image_tag: str) -> None:
    digest = subprocess.run([sys.executable, str(DIGEST_SCRIPT), "print", str(PROFILE)],
                            capture_output=True, text=True, check=True).stdout.strip()
    done = _sh(image_tag, "ls -1 /opt/casa/run && test -f /opt/casa/run/python3 "
                          "&& test ! -L /opt/casa/run/python3 && test -x /opt/casa/run/python3 "
                          "&& test -x /usr/bin/setpriv "
                          "&& cmp /opt/casa/run/python3 \"$(readlink -f /usr/bin/python3)\"")
    assert done.returncode == 0, done.stderr
    # A stale local __pycache__ in the build context is not the box's: nothing runs from it.
    listed = sorted(name for name in done.stdout.split() if name != "__pycache__")
    assert listed == sorted(["child.py", "python3", f"pyrun.{digest}"])


def test_the_images_profile_copy_is_the_committed_text(image_tag: str) -> None:
    done = _sh(image_tag, "cat /etc/casa-apparmor.txt")
    assert done.returncode == 0
    assert done.stdout == PROFILE.read_text(encoding="utf-8")


def test_the_dedicated_interpreter_runs_the_prelude_and_refuses_outside_a_box(image_tag: str) -> None:
    """In a plain container there is no child profile, so the prelude refuses before the
    script with no ready frame; it is a real interpreter that reaches the stdlib."""
    done = _sh(image_tag, "echo '{\"script\": \"print(1)\", \"sections\": []}' | "
                          "env -i /opt/casa/run/python3 -I -S -B /opt/casa/run/child.py 1 x 0; "
                          "echo \"rc=$?\"")
    assert "rc=10" in done.stdout and "box: " in done.stderr
    assert '{"box"' not in done.stdout
