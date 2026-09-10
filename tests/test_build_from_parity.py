"""Guard: the e2e image and the release image name the SAME base reference.

Pure-unit static text parity — no docker, no network, in the shape of
`tests/test_cli_sdk_pin_assert.py`'s Dockerfile-parity test.

**What this asserts and what it cannot.** It compares the two `ARG BUILD_FROM=`
lines as STRINGS and never resolves either of them. So it asserts that the two
images are built from the same *reference*; it says nothing about what that
reference resolves to today, and it cannot detect a stale digest, an upstream
re-tag, or any other freshness property. Recording the base a build actually
resolved is the job of the `io.hass.version` label printed by the workflow's
image-build steps, not of this test.

#937: `test-local/Dockerfile.test` was pinned by digest to docker-base 2026.04.0
while `casa/Dockerfile` floated on `base-debian:bookworm`, which had resolved to
2026.08.0 for three weeks. Nothing in the tree compared the two, so every e2e
tier certified a base the release image had not used since 2026-08-19.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RELEASE_DOCKERFILE = REPO / "casa" / "Dockerfile"
E2E_DOCKERFILE = REPO / "test-local" / "Dockerfile.test"

_PREFIX = "ARG BUILD_FROM="


def _build_from_lines(path: Path) -> list[str]:
    """Every line beginning exactly `ARG BUILD_FROM=`, unstripped.

    No whitespace stripping and no newline normalisation: a trailing space on
    one side is a real difference between two build arguments, and collapsing
    it would let the two files drift by exactly the amount this test exists to
    catch.
    """
    text = path.read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line.startswith(_PREFIX)]


def test_e2e_build_from_matches_release() -> None:
    release_lines = _build_from_lines(RELEASE_DOCKERFILE)
    e2e_lines = _build_from_lines(E2E_DOCKERFILE)

    # Counted, not merely truthy: a duplicated or deleted declaration is a
    # different defect from an unequal one, and it must not read as parity.
    assert (len(release_lines), len(e2e_lines)) == (1, 1), (
        f"expected exactly one {_PREFIX!r} line in each Dockerfile, got "
        f"{len(release_lines)} in {RELEASE_DOCKERFILE.name} and "
        f"{len(e2e_lines)} in {E2E_DOCKERFILE.name}"
    )
    assert e2e_lines[0] == release_lines[0], (
        "BUILD_FROM lines differ: the e2e image is built from a different base "
        f"than the released image.\n  {RELEASE_DOCKERFILE}: {release_lines[0]}\n"
        f"  {E2E_DOCKERFILE}: {e2e_lines[0]}"
    )
