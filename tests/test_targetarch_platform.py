"""Guard: neither Dockerfile shadows BuildKit's `TARGETARCH` (#982).

Pure-unit static text, in the shape of `tests/test_build_from_parity.py`.

`casa/Dockerfile` declares a bare `ARG TARGETARCH`, so BuildKit supplies the
target platform's value and the ttyd `case` below it picks the matching binary
for the published image. `test-local/Dockerfile.test` mirrors that file, but
declared `ARG TARGETARCH=amd64`: an explicit default shadows BuildKit's
predefined value, so an arm64 build of the e2e image installed `ttyd.x86_64`.

**What this asserts and what it cannot.** It pins only the TARGETARCH
declaration line. The CI jobs that build these images run on amd64, where both
declaration forms resolve to amd64, so this test does not establish a built
image's architecture. It pins both directions of drift: a default coming back
into `Dockerfile.test`, and the forbidden "consistency" repair of copying a
default into `casa/Dockerfile`, whose bareness selects the aarch64 ttyd for the
released arm image.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_DECLARATION = re.compile(r"^\s*ARG\s+TARGETARCH(?=\s|=|$)", re.I)


def _declarations(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line for line in text.split("\n") if _DECLARATION.match(line)]


@pytest.mark.parametrize(
    "dockerfile",
    [REPO / "casa" / "Dockerfile", REPO / "test-local" / "Dockerfile.test"],
    ids=["casa-Dockerfile", "test-local-Dockerfile.test"],
)
def test_targetarch_is_declared_once_without_default(dockerfile: Path) -> None:
    declarations = _declarations(dockerfile)
    assert len(declarations) == 1, (dockerfile, declarations)
    assert re.fullmatch(r"ARG\s+TARGETARCH", declarations[0].strip(), re.I), (
        f"{dockerfile.relative_to(REPO)} declares {declarations[0].strip()!r}: "
        "an ARG default shadows BuildKit's per-platform TARGETARCH (#982)"
    )
