"""Smoke guard for the live invariant auditor (test-local/audit/).

The auditor runs in-container against prod (block N / sweep #6); this just
ensures it stays importable/runnable and degrades gracefully when its inputs
are absent (so a syntax/logic regression fails in CI, not only in a live run).
Real behaviour is validated by running it live post-deploy.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

AUDITOR = Path(__file__).resolve().parents[1] / "test-local" / "audit" / "live_invariant_audit.py"


def test_auditor_runs_and_degrades_gracefully(tmp_path):
    """Against empty/absent inputs the auditor must not crash: it WARNs/skips
    (no config-sync report, validator import unavailable off
    the container) and prints a SUMMARY. Exit code = FAIL count."""
    env = {
        "CASA_CONFIG_DIR": str(tmp_path / "config"),
        "CASA_DATA_DIR": str(tmp_path / "data"),
        "PATH": "/usr/bin:/bin",
    }
    r = subprocess.run(
        [sys.executable, str(AUDITOR)],
        capture_output=True, text=True, env=env,
    )
    assert "SUMMARY:" in r.stdout, r.stdout + r.stderr
    # No hard FAIL when inputs are simply absent (schema validator unavailable
    # off-container → WARN, not FAIL).
    assert r.returncode == 0, f"unexpected FAILs on empty inputs:\n{r.stdout}"
