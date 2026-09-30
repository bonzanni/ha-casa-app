"""Pinned Claude CLI identity used by Casa's in-process SDK agents."""

from __future__ import annotations

import subprocess


CLAUDE_CLI_PATH = "/usr/local/bin/claude"
CLAUDE_CLI_VERSION = "2.1.273"

# #1111: the SDK fails the whole query on any stream-json line longer than
# ``max_buffer_size`` (1 MiB when unset). A built-in ``Read`` of a PDF carries
# the file's base64 twice on one line (the tool result's document block and
# ``tool_use_result``), so a ~400 KB PDF already overflowed the default. The
# pinned CLI 2.1.273 inlines a whole PDF up to 20 MiB and refuses a larger one
# (above 3 MiB it first tries ``pdftoppm`` page rendering, absent from this
# image, and falls back to inlining). Two base64 copies of 20 MiB are ~53.4 MiB,
# so 64 MiB leaves margin for the envelope. Page-range reads always render
# through ``pdftoppm`` and so fail here; adding poppler to the image would need
# this value re-sized (up to 20 pages x 5 MiB base64). The bound only caps one
# line — nothing is preallocated.
SDK_MAX_BUFFER_SIZE = 64 * 1024 * 1024


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
