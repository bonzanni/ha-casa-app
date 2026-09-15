"""The memory cage joins the systemd slice a supervising caller names.

A `systemd-run --scope` started from inside another scope is registered as a
SIBLING under `app.slice`, never as a child, so a supervisor that watches its
own cgroup subtree cannot see it: it reports the suite finished while the
workers are still running. A caller that owns a slice exports `DRIVE_SLICE`,
and the cage's scope then joins that slice with `--slice=`. When the variable
is unset the cage is exactly what it was.

Two tests: the definition carries the conditional (runs anywhere), and the
expanded command carries `--slice=` exactly when the variable is set (needs
`make` and a user bus, since `CAGE` is empty without one; skipped otherwise,
and a skip here is not evidence — the first test is the one that always runs).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
RIDER = "$(if $(DRIVE_SLICE),--slice=$(DRIVE_SLICE) )"


def _cage_definition() -> str:
    lines = [ln for ln in MAKEFILE.read_text().splitlines() if ln.startswith("CAGE :=")]
    assert len(lines) == 1, lines
    return lines[0]


def test_the_cage_definition_joins_the_slice_the_caller_names():
    cage = _cage_definition()
    assert RIDER in cage, cage
    assert cage.index(RIDER) < cage.index("-p MemoryMax="), "the rider must precede the properties"


def _user_bus() -> bool:
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "-q", "true"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(shutil.which("make") is None or not _user_bus(),
                    reason="needs make and a systemd user bus: CAGE is empty without one")
def test_the_expanded_cage_carries_the_slice_exactly_when_named():
    def cage(env: dict) -> str:
        r = subprocess.run(
            ["make", "-s", "-C", str(ROOT), '--eval=print-cage: ; @echo "$(CAGE)"', "print-cage"],
            env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()
    # MAKEFLAGS/MFLAGS from an enclosing `make test-unit` would make this a
    # sub-make with a jobserver it cannot reach; the probe is standalone.
    base = {k: v for k, v in os.environ.items() if k not in ("DRIVE_SLICE", "MAKEFLAGS", "MFLAGS")}
    plain = cage(base)
    assert plain.startswith("systemd-run --user --scope -q -p MemoryMax=") and "--slice" not in plain, plain
    joined = cage({**base, "DRIVE_SLICE": "drive-abcdefabcdef.slice"})
    assert "systemd-run --user --scope -q --slice=drive-abcdefabcdef.slice -p MemoryMax=" in joined, joined
