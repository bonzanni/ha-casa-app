"""The e2e image's `TARGETARCH` declaration follows the build platform (#982).

`tests/test_targetarch_platform.py` pins the declaration's TEXT. This test
checks what that text does under BuildKit. It takes the real `ARG TARGETARCH`
line from `test-local/Dockerfile.test`, puts it in a `FROM scratch` image that
records the value in a label, builds that image for `linux/arm64`, and requires
the label to read `arm64`. A defaulted declaration (`ARG TARGETARCH=amd64`)
records `amd64` there.

**What this asserts and what it cannot.** It pins how the declaration
RESOLVES, one step short of the e2e image itself. It does not build that image
or inspect its ttyd binary, and no amd64 runner can. `FROM scratch` executes no
instruction, so the build should need no emulator; that could not be proven on
the development host, which has a qemu-aarch64 handler registered. Runs in
qa.yml's `baseline-runtime` job, which names this file.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.docker]

REPO = Path(__file__).resolve().parents[1]
E2E_DOCKERFILE = REPO / "test-local" / "Dockerfile.test"

_DECLARATION = re.compile(r"^\s*ARG\s+TARGETARCH(?=\s|=|$)", re.I)


def test_e2e_targetarch_declaration_follows_the_build_platform(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    lines = [line for line in E2E_DOCKERFILE.read_text(encoding="utf-8").split("\n")
             if _DECLARATION.match(line)]
    assert len(lines) == 1, lines
    (tmp_path / "Dockerfile").write_text(
        "FROM scratch\n"
        f"{lines[0].strip()}\n"
        'LABEL casa.targetarch="${TARGETARCH}"\n',
        encoding="utf-8",
    )
    tag = f"casa-targetarch-probe:{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            ["docker", "build", "-q", "--platform", "linux/arm64", "-t", tag,
             str(tmp_path)],
            check=True, capture_output=True, text=True,
        )
        r = subprocess.run(
            ["docker", "image", "inspect", tag,
             "--format", '{{index .Config.Labels "casa.targetarch"}}'],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "arm64", (lines[0], r.stdout)
    finally:
        subprocess.run(["docker", "image", "rm", tag],
                       capture_output=True, check=False)
