"""Pinned Claude CLI identity used by Casa's in-process SDK agents."""

from __future__ import annotations

import subprocess


CLAUDE_CLI_PATH = "/usr/local/bin/claude"
CLAUDE_CLI_VERSION = "2.1.273"


def verify_effective_cli() -> str:
    """Return the effective CLI version string or fail closed on mismatch."""
    proc = subprocess.run(
        [CLAUDE_CLI_PATH, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    rendered = (proc.stdout or proc.stderr or "").strip()
    observed_version = rendered.split(maxsplit=1)[0] if rendered else ""
    if proc.returncode != 0 or observed_version != CLAUDE_CLI_VERSION:
        raise RuntimeError(
            f"effective Claude CLI mismatch: expected {CLAUDE_CLI_VERSION}, "
            f"path={CLAUDE_CLI_PATH}, returncode={proc.returncode}"
        )
    return rendered
